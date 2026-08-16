#!/usr/bin/env python
"""Build the lexical index over an ingested run's documents.

PLAN.md's division of labour: lexical search finds entry points, the graph does
the relationship reasoning that turns those entry points into an answer. Without
this index the controller has to guess entry points from claim text, which is a
much narrower net and abstains on questions the corpus can answer.

The index is contentless — bodies stay in Parquet — and its rowid is the
document's 63-bit graph id, so a hit joins straight to the graph with no
intermediate mapping.

    uv run python scripts/25_build_slice_fts.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config, fts  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.parsers import normalise_content  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--db", default=None, help="defaults to config.FTS_DB")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else config.FTS_DB

    with HydraClient() as client:
        client.verify()
        run_id = args.run_id
        if not run_id:
            rows = client.bolt_read(
                "MATCH (d:Document) RETURN d.run_id AS run_id "
                "ORDER BY run_id DESC LIMIT 1")
            run_id = rows[0]["run_id"] if rows else None
        if not run_id:
            print("no ingested run found", file=sys.stderr)
            return 1

        rows = client.bolt_read(
            "MATCH (d:Document) WHERE d.run_id = $r "
            "RETURN d.dsid AS dsid, d.id AS id, d.title AS title ORDER BY d.dsid",
            {"r": run_id})
        by_dsid = {row["dsid"]: row for row in rows}
        print(f"run {run_id}: indexing {len(by_dsid)} documents into {db_path}")

    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    bodies: dict[str, str] = {}
    for batch in parquet.iter_batches(batch_size=4000, columns=["doc_id", "content"]):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            dsid = data["doc_id"][i]
            if dsid in by_dsid:
                bodies[dsid] = normalise_content(data["content"][i])
        if len(bodies) == len(by_dsid):
            break

    # Ordered by dsid so a resumed build sees the same rows in the same order;
    # resumption in a contentless index is positional and cannot be repaired
    # after the fact.
    index_rows = [
        (by_dsid[dsid]["id"], by_dsid[dsid]["title"] or "", bodies[dsid])
        for dsid in sorted(bodies)
    ]
    stats = fts.build_index(index_rows, db_path=db_path)
    print(f"  indexed {stats}")
    print(f"  {fts.index_stats(db_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
