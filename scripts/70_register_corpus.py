#!/usr/bin/env python
"""Index the whole corpus lexically, and register its ids.

Corpus scale lives in the lexical index and in Parquet, not in the graph. A
HydraDB label index holds at most 250,000 vertices, and pushing all 511,962
documents into one label does not merely fail — the partial write leaves the
label over its cap and every unbounded scan of it fails afterwards, including
the delete that would undo it (docs/engine-notes.md).

That constraint points where PLAN.md already did: Parquet stays the
authoritative full-text store, lexical search covers everything, and the graph
holds the enriched working set — the documents that carry mentions, claims, and
evidence. So this script builds the index over all 511,962 documents and
registers their deterministic ids, and writes graph nodes only when asked, with
a cap.

    uv run python scripts/70_register_corpus.py              # index + ids only
    uv run python scripts/70_register_corpus.py --graph 50000  # also 50k nodes
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config, fts  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import IdRegistry, node_identity  # noqa: E402
from tracegraph.loader import Checkpointer, upsert_nodes  # noqa: E402
from tracegraph.parsers import normalise_content  # noqa: E402

CORPUS_RUN = "corpus"

# Admission control rejects a label index past this size, and the partial write
# leaves the label unscannable. Stay well below it.
LABEL_CEILING = 200_000


def stream(limit: int):
    """Yield documents, normalising bodies once for both consumers."""
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    seen = 0
    for batch in parquet.iter_batches(
        batch_size=2000, columns=["doc_id", "source_type", "title", "content"]
    ):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            yield (data["doc_id"][i], data["source_type"][i],
                   data["title"][i] or "", normalise_content(data["content"][i]))
            seen += 1
            if limit and seen >= limit:
                return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="0 means the whole corpus")
    ap.add_argument("--graph", type=int, default=0,
                    help="how many Document nodes to write; 0 writes none. "
                         "Kept under the 250,000 label-index ceiling.")
    ap.add_argument("--skip-fts", action="store_true")
    args = ap.parse_args()

    if args.graph > LABEL_CEILING:
        print(f"--graph {args.graph} exceeds the {LABEL_CEILING:,} label-index "
              "ceiling; a partial write would wedge the label", file=sys.stderr)
        return 1

    registry = IdRegistry()
    checkpointer = Checkpointer()
    started = time.perf_counter()

    node_rows: list[dict] = []
    index_rows: list[tuple[int, str, str]] = []
    identities = []
    count = 0

    for dsid, source_type, title, body in stream(args.limit):
        identity = node_identity("Document", dsid)
        identities.append(identity)
        node_rows.append({
            "vertex": identity.id, "dsid": dsid, "source_type": source_type,
            "title": title[:500], "run_id": CORPUS_RUN,
        })
        index_rows.append((identity.id, title, body))
        count += 1
        if count % 50000 == 0:
            print(f"  read {count:,} documents "
                  f"({count / (time.perf_counter() - started):,.0f}/s)")

    print(f"read {count:,} documents in {time.perf_counter() - started:.1f}s")

    # Registering every id up front means a collision stops the run before a
    # single node is written, rather than halfway through.
    registry.register_many(identities)
    print(f"registry holds {registry.count('node'):,} node identities")

    if args.graph:
        t0 = time.perf_counter()
        node_rows = node_rows[: args.graph]
        with HydraClient() as client:
            client.verify()
            upsert_nodes(client, "Document", node_rows, job=f"corpus:{len(node_rows)}",
                         properties=["dsid", "source_type", "title", "run_id"],
                         checkpointer=checkpointer)
            total = client.bolt_read(
                "MATCH (d:Document) WHERE d.run_id = $r RETURN count(*) AS n",
                {"r": CORPUS_RUN})
        elapsed = time.perf_counter() - t0
        print(f"graph: {total[0]['n']:,} Document nodes "
              f"({len(node_rows) / max(elapsed, 0.001):,.0f}/s)")

    if not args.skip_fts:
        t0 = time.perf_counter()
        # Ordered by the same stream on every run: resumption in a contentless
        # index is positional and cannot be repaired afterwards.
        stats = fts.build_index(index_rows, db_path=config.FTS_DB)
        print(f"index: {stats.rows_indexed:,} rows in "
              f"{time.perf_counter() - t0:.1f}s")
        print(f"  {fts.index_stats(config.FTS_DB)}")

    checkpointer.close()
    print(f"total {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
