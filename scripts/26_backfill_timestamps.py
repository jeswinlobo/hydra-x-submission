#!/usr/bin/env python
"""Put real document timestamps on Document nodes.

Recency is the component that decides a mutable fact — which job title someone
holds *now* — and it is worthless without real time. Ordering documents by dsid,
as a first pass did, ranks them alphabetically and dresses the result up as
chronology, which is worse than having no recency at all because it looks like
evidence.

Gmail states a `Date:` header and Fireflies a meeting date. Both are parsed
already; this reads them back and stores an epoch on the Document, leaving
documents that state no date without one rather than inventing a position.

    uv run python scripts/26_backfill_timestamps.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.loader import upsert_nodes  # noqa: E402
from tracegraph.parsers import parse_document  # noqa: E402

ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%b %d, %Y", "%d %b %Y",
)


def to_epoch(raw: str | None) -> int | None:
    """Parse a stated date to an epoch, or decline.

    Several formats appear across sources, and a date that cannot be read is
    left absent. Guessing one would put a document at a position in time it
    never claimed.
    """
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ISO_FORMATS:
        try:
            parsed = datetime.strptime(text[:32].strip(), fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

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
            "RETURN d.dsid AS dsid, d.id AS id, d.source_type AS st, d.title AS title",
            {"r": run_id})
        by_dsid = {row["dsid"]: row for row in rows}
        print(f"run {run_id}: {len(by_dsid)} documents")

        parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
        updates, dated, undated = [], 0, 0
        for batch in parquet.iter_batches(
            batch_size=4000, columns=["doc_id", "source_type", "title", "content"]
        ):
            data = batch.to_pydict()
            for i in range(batch.num_rows):
                dsid = data["doc_id"][i]
                node = by_dsid.get(dsid)
                if node is None:
                    continue
                parsed = parse_document(dsid, data["source_type"][i],
                                        data["title"][i], data["content"][i])
                epoch = to_epoch(parsed.attributes.get("date"))
                if epoch is None:
                    undated += 1
                    continue
                dated += 1
                updates.append({"vertex": node["id"], "timestamp": epoch})
            if len(updates) + undated >= len(by_dsid):
                break

        print(f"  {dated} documents state a parseable date, {undated} do not")
        if updates:
            upsert_nodes(client, "Document", updates, job=f"timestamps:{run_id}",
                         properties=["timestamp"])
            span = client.bolt_read(
                "MATCH (d:Document) WHERE d.run_id = $r AND d.timestamp > 0 "
                "RETURN count(*) AS n", {"r": run_id})
            print(f"  {span[0]['n']} Document nodes now carry a timestamp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
