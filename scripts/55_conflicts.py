#!/usr/bin/env python
"""Detect contested facts, weigh their versions, and record them in the graph.

Conflicts become edges rather than a report, so the answer controller can find
them by traversal. CONFLICTS_WITH is logically symmetric while the engine's
relationships are directed, so exactly one edge is written, from the lower claim
id to the higher, and readers query both directions. One deterministic edge per
pair keeps replay idempotent; two would have to be kept in step.

    uv run python scripts/55_conflicts.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph.conflicts import ClaimRecord, detect_conflicts
from tracegraph.reconcile import load_subject_identity, prune_superseded  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import IdRegistry, edge_identity  # noqa: E402
from tracegraph.loader import Checkpointer, upsert_edges  # noqa: E402


def load_claims(client: HydraClient, run_id: str) -> list[ClaimRecord]:
    rows = client.bolt_read(
        "MATCH (d:Document)-[:ASSERTS]->(c:Claim)-[:SUPPORTED_BY]->(s:EvidenceSpan) "
        "WHERE c.run_id = $r "
        "RETURN c.id AS claim_id, c.dsid AS dsid, d.source_type AS source_type, "
        "c.subject AS subject, c.predicate AS predicate, c.object AS object, "
        "c.confidence AS confidence, s.quote AS quote, d.timestamp AS timestamp "
        "LIMIT 8000",
        {"r": run_id})
    return [ClaimRecord(**row) for row in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--show", type=int, default=6)
    args = ap.parse_args()

    registry = IdRegistry()
    checkpointer = Checkpointer()

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

        records = load_claims(client, run_id)

        # Oldest first, by the date the document itself states. Documents with
        # no stated date are excluded from the ordering rather than placed
        # arbitrarily: a position they never claimed would be indistinguishable
        # from evidence once it reached the recency score.
        timestamps = {r.dsid: r.timestamp for r in records
                      if getattr(r, "timestamp", None)}
        order = sorted(timestamps, key=lambda d: timestamps[d])
        print(f"  {len(order)} of {len({r.dsid for r in records})} documents "
              "carry a stated date and take part in recency")
        # Grouped by resolved identity where the graph knows one, so a
        # disagreement cannot be assembled out of two different people who share
        # a name — Anna Liu at cedarwave.com and Anna Liu at cloudwave.com are
        # not two versions of one person's employer.
        identity = load_subject_identity(client, run_id)
        print(f"  {len(identity)} resolved surfaces available to group by identity")
        conflicts, stats = detect_conflicts(
            records, document_order=order, subject_identity=identity)

        print(f"run {run_id}: {len(records)} claims")
        print(f"  {stats['groups_examined']} single-valued subject+predicate groups")
        print(f"  {stats['conflicts_found']} contested facts, "
              f"{stats['decided']} with a best-supported version")
        print(f"  {stats['unmapped_predicates']} raw predicates unmapped "
              f"(queued, not guessed)")
        if stats["top_unmapped"]:
            print(f"  most frequent unmapped: "
                  f"{[p for p, _ in stats['top_unmapped'][:5]]}")

        # --- write the conflict graph ---------------------------------------
        pending, edges = [], []
        seen: set[tuple[int, int]] = set()
        for conflict in conflicts:
            claim_ids = sorted(
                {c.claim_id for version in conflict.versions for c in version.claims})
            for i, left in enumerate(claim_ids):
                for right in claim_ids[i + 1:]:
                    if (left, right) in seen:
                        continue
                    seen.add((left, right))
                    identity = edge_identity(
                        "CONFLICTS_WITH", left, right, conflict.predicate.name)
                    pending.append(identity)
                    edges.append({
                        "src": left, "dst": right, "eid": identity.id,
                        "predicate": conflict.predicate.name,
                        "subject": conflict.subject[:200],
                        "decided": bool(conflict.decided),
                        "margin": round(conflict.margin, 4),
                        "run_id": run_id,
                    })

        if edges:
            registry.register_many(pending)
            upsert_edges(client, "CONFLICTS_WITH", edges,
                         job=f"conflicts:{run_id}:{len(edges)}",
                         source_label="Claim", target_label="Claim",
                         properties=["predicate", "subject", "decided", "margin",
                                     "run_id"],
                         checkpointer=checkpointer)

        # A sweep is authoritative, so it also removes what it no longer finds.
        # Without this an edge written under a superseded rule survives forever
        # and the answer path keeps reporting it — which is how thirty-one
        # disputes between two different people stayed in the graph after the
        # grouping was corrected.
        removed = prune_superseded(client, run_id, {(e["src"], e["dst"]) for e in edges})
        total = client.bolt_read(
            "MATCH (a:Claim)-[r:CONFLICTS_WITH]->(b:Claim) WHERE r.run_id = $r "
            "RETURN count(*) AS n", {"r": run_id})
        print(f"  {len(edges)} CONFLICTS_WITH edges written, {removed} superseded "
              f"removed, {total[0]['n']} in graph")

        # --- show the interesting ones ---------------------------------------
        for conflict in conflicts[: args.show]:
            print(f"\n  {conflict.subject} — {conflict.predicate.name}"
                  f"{' (mutable)' if conflict.predicate.mutable else ''}")
            for version in conflict.versions:
                mark = "*" if version is conflict.best else " "
                trust = version.trust
                print(f"   {mark} {version.display[:52]:54} "
                      f"trust={trust.score:.3f} "
                      f"(authority={trust.authority} corroboration={trust.corroboration} "
                      f"directness={trust.directness} recency={trust.recency})")
                print(f"       {version.sources} {version.dsids[:2]}")
            print(f"     -> {conflict.reason}")

    checkpointer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
