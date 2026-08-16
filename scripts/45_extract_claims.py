#!/usr/bin/env python
"""Extract claims for an ingested run and load them as provenance.

Claims arrive from the model; evidence spans are what make them checkable. Both
become first-class nodes rather than properties, because the UI has to be able
to show a span on its own and because one span can support several claims.

Only documents already in the graph are extracted, so every claim has a
Document to attach to, and a claim whose span is not verbatim in its source is
never written at all — it is dropped at the boundary, not stored with a flag.

    uv run python scripts/45_extract_claims.py --docs 40
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config, fts  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import IdRegistry, edge_identity, node_identity  # noqa: E402
from tracegraph.llm import (  # noqa: E402
    build_batch_requests,
    collect_batch_results,
    poll_batch,
    submit_batch,
)
from tracegraph.loader import Checkpointer, upsert_edges, upsert_nodes  # noqa: E402
from tracegraph.parsers import normalise_content  # noqa: E402

CLAIM = "Claim"
SPAN = "EvidenceSpan"
DOC = "Document"


def latest_run(client: HydraClient) -> str | None:
    rows = client.bolt_read(
        "MATCH (d:Document) RETURN d.run_id AS run_id ORDER BY run_id DESC LIMIT 1")
    return rows[0]["run_id"] if rows else None


def documents_in_run(client: HydraClient, run_id: str, limit: int) -> dict[str, int]:
    """dsid -> node id, for documents this run already loaded."""
    rows = client.bolt_read(
        "MATCH (d:Document) WHERE d.run_id = $r RETURN d.dsid AS dsid, d.id AS id "
        f"ORDER BY dsid LIMIT {int(limit)}", {"r": run_id})
    return {row["dsid"]: row["id"] for row in rows}


def load_bodies(dsids: set[str]) -> dict[str, str]:
    """Fetch the normalised bodies for a set of dsids in one streaming pass."""
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    out: dict[str, str] = {}
    for batch in parquet.iter_batches(
        batch_size=4000, columns=["doc_id", "content"]
    ):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            dsid = data["doc_id"][i]
            if dsid in dsids:
                out[dsid] = normalise_content(data["content"][i])[:8000]
        if len(out) == len(dsids):
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--docs", type=int, default=40,
                    help="documents to extract from")
    ap.add_argument("--for-questions", nargs="*", default=None,
                    help="extract for the documents these questions retrieve, "
                         "which is PLAN.md's tier-3 pass: spend the budget where "
                         "users actually land rather than on the longest files")
    args = ap.parse_args()

    registry = IdRegistry()
    checkpointer = Checkpointer()

    with HydraClient() as client:
        client.verify()
        run_id = args.run_id or latest_run(client)
        if not run_id:
            print("no ingested run; run scripts/30_load_slice.py first", file=sys.stderr)
            return 1

        doc_ids = documents_in_run(client, run_id, 100000)

        if args.for_questions:
            # Retrieval-driven selection: index rowids are graph ids, so an FTS
            # hit maps straight back to a Document node.
            by_node = {node: dsid for dsid, node in doc_ids.items()}
            wanted: list[str] = []
            for question in args.for_questions:
                for node_id, _ in fts.search(question, limit=args.docs):
                    dsid = by_node.get(node_id)
                    if dsid and dsid not in wanted:
                        wanted.append(dsid)
            selected = wanted[: args.docs]
            print(f"  {len(selected)} documents retrieved by "
                  f"{len(args.for_questions)} question(s)")
            bodies = load_bodies(set(selected))
            texts = {dsid: bodies[dsid] for dsid in selected if dsid in bodies}
        else:
            bodies = load_bodies(set(list(doc_ids)[: args.docs * 4]))
            # Longer documents carry more claims per request.
            texts = dict(sorted(bodies.items(), key=lambda kv: -len(kv[1]))[: args.docs])
        print(f"run {run_id}: extracting from {len(texts)} of {len(doc_ids)} documents")

        requests = build_batch_requests(texts.items())
        batch = submit_batch(requests)
        print(f"  batch {batch.id}; polling")
        ended = poll_batch(batch.id)
        print(f"  {ended.processing_status}: {ended.request_counts}")

        result = collect_batch_results(batch.id, texts)
        manifest = result.manifest
        print(f"  accepted {manifest.accepted_claims}, "
              f"rejected {manifest.rejected_claims}, "
              f"failed documents {len(result.failures)}")
        if result.rejected:
            print(f"  rejections: {dict(Counter(r.reason for r in result.rejected))}")
        if not result.accepted:
            print("no claims survived validation", file=sys.stderr)
            return 1

        # Skip documents whose claims are already in the graph, so a re-run
        # after adding a question does not pay for the overlap again.
        # --- build nodes and edges -------------------------------------------
        pending, claim_rows, span_rows = [], [], []
        asserts, supported_by = [], []
        seen_spans: dict[str, int] = {}

        for claim in result.accepted:
            doc_node = doc_ids.get(claim.doc_id)
            if doc_node is None:
                continue

            claim_key = (f"{claim.doc_id}|{claim.subject}|{claim.predicate}"
                         f"|{claim.object}|{claim.span_start}")
            claim_row = node_identity(CLAIM, claim_key)
            pending.append(claim_row)
            claim_rows.append({
                "vertex": claim_row.id, "dsid": claim.doc_id,
                "subject": claim.subject[:200], "predicate": claim.predicate[:120],
                "object": claim.object[:200], "object_type": claim.object_type,
                "confidence": float(claim.confidence), "run_id": run_id,
            })

            # One span can support several claims, so it is deduplicated by its
            # own identity — document plus offsets — rather than per claim.
            span_key = f"{claim.doc_id}:{claim.span_start}:{claim.span_end}"
            if span_key not in seen_spans:
                span_row = node_identity(SPAN, span_key)
                pending.append(span_row)
                seen_spans[span_key] = span_row.id
                span_rows.append({
                    "vertex": span_row.id, "dsid": claim.doc_id,
                    "start": int(claim.span_start), "end": int(claim.span_end),
                    "quote": claim.evidence_span[:900], "run_id": run_id,
                })
            span_node = seen_spans[span_key]

            a = edge_identity("ASSERTS", doc_node, claim_row.id)
            pending.append(a)
            asserts.append({"src": doc_node, "dst": claim_row.id, "eid": a.id,
                            "run_id": run_id})

            s = edge_identity("SUPPORTED_BY", claim_row.id, span_node)
            pending.append(s)
            supported_by.append({"src": claim_row.id, "dst": span_node, "eid": s.id,
                                 "run_id": run_id})

        registry.register_many(pending)

        # Keyed by content, not just by run: a second extraction pass over
        # different documents in the same run is different work, and a job name
        # that ignores that skips every batch as already complete.
        stamp = hashlib.sha256(
            "".join(sorted(r["dsid"] for r in claim_rows)).encode()
        ).hexdigest()[:12]

        upsert_nodes(client, CLAIM, claim_rows, job=f"claims:{run_id}:{stamp}",
                     properties=["dsid", "subject", "predicate", "object",
                                 "object_type", "confidence", "run_id"],
                     checkpointer=checkpointer)
        upsert_nodes(client, SPAN, span_rows, job=f"spans:{run_id}:{stamp}",
                     properties=["dsid", "start", "end", "quote", "run_id"],
                     checkpointer=checkpointer)
        upsert_edges(client, "ASSERTS", asserts, job=f"asserts:{run_id}:{stamp}",
                     source_label=DOC, target_label=CLAIM,
                     properties=["run_id"], checkpointer=checkpointer)
        upsert_edges(client, "SUPPORTED_BY", supported_by,
                     job=f"supported:{run_id}:{stamp}", source_label=CLAIM,
                     target_label=SPAN, properties=["run_id"],
                     checkpointer=checkpointer)

        print(f"\n  {len(claim_rows)} claims, {len(span_rows)} distinct spans")
        for label in (CLAIM, SPAN):
            rows = client.bolt_read(
                f"MATCH (n:{label}) WHERE n.run_id = $r RETURN count(*) AS c",
                {"r": run_id})
            print(f"  {label:14} {rows[0]['c']:>6} in graph")

        usage = manifest.total_usage
        cost = (usage.input_tokens / 1e6 * 1.0
                + usage.output_tokens / 1e6 * 5.0) * 0.5
        print(f"  batch-priced cost ${cost:.4f}")

    checkpointer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
