#!/usr/bin/env python
"""Re-chunk the corpus so fetching one document is a lookup, not a scan.

The corpus ships as a single row group holding all 511,962 documents. Parquet
decodes a row group whole, so a point lookup against it reads the entire 1.4 GB
file: four and a half seconds to fetch one document, paid four times per
question because on-demand ingestion enriches several candidates at once.
Answering spent longer scanning parquet than it did talking to the model.

This writes a losslessly re-chunked copy — same rows, same schema, same order,
2,048 rows per row group — and builds the row map against it. The original is
never touched and stays the source of truth for bulk passes, which stream whole
row groups and are indifferent to the layout.

    uv run python scripts/71_repartition_corpus.py
    uv run python scripts/71_repartition_corpus.py --check   # measure only

Resumable and safe to re-run: the copy is written to a temporary path and moved
into place only when complete, and the row map skips groups already indexed.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config  # noqa: E402
from tracegraph.parquet_reader import RowLocator, repartition  # noqa: E402


def measure(parquet_path: Path, db_path: Path, samples: int = 5) -> float:
    """Median seconds to fetch one document. The number this script exists for."""
    locator = RowLocator(parquet_path, db_path)
    try:
        rows = locator._conn.execute(
            "SELECT doc_id FROM doc_location ORDER BY doc_id LIMIT ?",
            (samples,)).fetchall()
        timings = []
        for (doc_id,) in rows:
            started = time.perf_counter()
            record = locator.fetch(doc_id)
            timings.append(time.perf_counter() - started)
            if record is None:
                raise SystemExit(f"{doc_id} is indexed but did not fetch")
        return statistics.median(timings)
    finally:
        locator.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report fetch latency without building anything")
    ap.add_argument("--rows-per-group", type=int, default=2048)
    args = ap.parse_args()

    source = config.DOCUMENTS_PARQUET
    if not source.exists():
        print(f"corpus not found at {source}", file=sys.stderr)
        return 1

    if args.check:
        median = measure(config.locator_parquet(), config.locator_db())
        print(f"fetch latency: {median * 1000:.0f}ms median "
              f"({config.locator_parquet().name})")
        return 0

    before = None
    if config.REGISTRY_DB.exists():
        try:
            before = measure(config.DOCUMENTS_PARQUET, config.REGISTRY_DB, samples=3)
            print(f"before: {before * 1000:.0f}ms median fetch "
                  f"({pq.ParquetFile(source).num_row_groups} row group(s))")
        except Exception as exc:  # noqa: BLE001 - a baseline is nice, not required
            print(f"  (no baseline: {exc})")

    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if config.LOCATOR_PARQUET.exists():
        print(f"re-chunked copy already at {config.LOCATOR_PARQUET}")
    else:
        started = time.perf_counter()
        # Written beside the target and moved into place, so an interrupted run
        # cannot leave a half-written file that looks complete to `locator_parquet`.
        staging = config.LOCATOR_PARQUET.with_suffix(".partial")
        repartition(source, staging, row_group_size=args.rows_per_group)
        staging.replace(config.LOCATOR_PARQUET)
        print(f"re-chunked in {time.perf_counter() - started:.0f}s → "
              f"{config.LOCATOR_PARQUET}")

    parquet = pq.ParquetFile(config.LOCATOR_PARQUET)
    print(f"  {parquet.metadata.num_rows} rows in "
          f"{parquet.num_row_groups} row groups")

    started = time.perf_counter()
    locator = RowLocator.build(config.LOCATOR_PARQUET, config.LOCATOR_DB)
    print(f"  row map: {locator.build_report} in {time.perf_counter() - started:.0f}s")
    locator.close()

    after = measure(config.LOCATOR_PARQUET, config.LOCATOR_DB)
    print(f"after: {after * 1000:.0f}ms median fetch")
    if before:
        print(f"  {before / after:.0f}x faster per document, "
              f"{(before - after) * 4:.1f}s saved per question at a budget of 4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
