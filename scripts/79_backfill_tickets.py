#!/usr/bin/env python
"""Write `Ticket` nodes and `REFERENCES` edges for documents already ingested.

Ticket keys have been extracted by every parser since the beginning and read by
nothing. New ingestion now writes them, but the documents already in the graph
predate that, so without this the traversal has nothing to walk on exactly the
working set a demo uses.

This is parsing only — no model calls, no cost, and it re-reads bodies from
parquet rather than trusting anything stored. Idempotent: node and edge ids are
deterministic, so a second run rewrites the same rows and changes nothing.

    uv run python scripts/79_backfill_tickets.py            # report only
    uv run python scripts/79_backfill_tickets.py --apply    # write
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph import config  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import edge_identity, node_identity  # noqa: E402
from tracegraph.ingest import DOC, TICKET, _ticket_keys  # noqa: E402
from tracegraph.loader import upsert_edges, upsert_nodes  # noqa: E402
from tracegraph.parquet_reader import RowLocator  # noqa: E402
from tracegraph.parsers import parse_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    with HydraClient() as client:
        client.verify()
        rows = client.bolt_read(
            "MATCH (d:Document) RETURN d.dsid AS dsid, d.source_type AS st, "
            "d.run_id AS run_id")
        if args.limit:
            rows = rows[: args.limit]
        print(f"{len(rows)} documents in the graph")

        locator = RowLocator(config.locator_parquet(), config.locator_db(),
                             require_complete=True)
        ticket_rows: dict[int, dict] = {}
        edges: list[dict] = []
        key_docs: dict[str, set[str]] = defaultdict(set)
        by_source: Counter = Counter()
        missing = 0

        try:
            for row in rows:
                dsid = row["dsid"]
                record = locator.fetch(dsid)
                if record is None:
                    missing += 1
                    continue
                parsed = parse_document(
                    dsid, row["st"] or "", record.get("title") or "",
                    record.get("content") or "")
                keys = _ticket_keys(parsed.references)
                if keys:
                    by_source[row["st"] or "(none)"] += 1
                doc_id = node_identity(DOC, dsid).id
                for key in keys:
                    key_docs[key].add(dsid)
                    identity = node_identity(TICKET, key)
                    ticket_rows[identity.id] = {
                        "vertex": identity.id, "key": key,
                        "run_id": row["run_id"],
                    }
                    edge = edge_identity("REFERENCES", doc_id, identity.id)
                    edges.append({
                        "src": doc_id, "dst": identity.id, "eid": edge.id,
                        "run_id": row["run_id"],
                    })
        finally:
            locator.close()

        shared = {k: v for k, v in key_docs.items() if len(v) >= 2}
        joined = set().union(*shared.values()) if shared else set()

        print(f"  documents not in corpus : {missing}")
        print(f"  distinct tickets        : {len(ticket_rows)}")
        print(f"  REFERENCES edges        : {len(edges)}")
        print(f"  tickets in >=2 documents: {len(shared)}")
        print(f"  documents so joined     : {len(joined)}")
        print("\n  documents carrying a ticket, by source:")
        for source, n in by_source.most_common():
            print(f"    {source:<16} {n}")

        if shared:
            print("\n  the traversals this creates:")
            for key, docs in sorted(shared.items(), key=lambda x: -len(x[1]))[:10]:
                print(f"    {key:<16} {len(docs)} documents")

        if not args.apply:
            print("\nreport only; pass --apply to write")
            return 0

        if ticket_rows:
            upsert_nodes(client, TICKET, list(ticket_rows.values()),
                         job="backfill-tickets", properties=["key", "run_id"])
            upsert_edges(client, "REFERENCES", edges, job="backfill-references",
                         source_label=DOC, target_label=TICKET,
                         properties=["run_id"])
            print(f"\nwrote {len(ticket_rows)} tickets, {len(edges)} edges")
        else:
            print("\nnothing to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
