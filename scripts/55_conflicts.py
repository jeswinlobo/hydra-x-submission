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
from tracegraph.neardup import canonical_map, find_near_duplicates  # noqa: E402
from tracegraph.reconcile import (  # noqa: E402
    conflict_edge_rows,
    load_claims as paged_claims,
    load_subject_identity,
    prune_superseded,
)  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import IdRegistry, edge_identity  # noqa: E402
from tracegraph.loader import Checkpointer, upsert_edges  # noqa: E402


def load_claims(client: HydraClient, run_id: str) -> list[ClaimRecord]:
    """Every claim in the run, through the same paged reader the incremental
    pass uses — a flat LIMIT here would make the authoritative sweep judge each
    dispute against an arbitrary subset the moment the graph outgrew it."""
    return [ClaimRecord(**row) for row in paged_claims(client, run_id)]


def near_duplicate_map(client, run_id: str) -> dict[str, str]:
    """dsid -> canonical member of its near-duplicate cluster.

    Bodies are re-read from parquet rather than the graph, because parquet is
    the authoritative text store and the graph deliberately does not copy bodies
    into it. Returns an empty map — never raises — if the corpus is unavailable,
    since a missing near-duplicate discount degrades corroboration slightly
    rather than making the sweep wrong.
    """
    try:
        from tracegraph import config
        from tracegraph.parquet_reader import RowLocator
        from tracegraph.parsers import normalise_content

        rows = client.bolt_read(
            "MATCH (d:Document) WHERE d.run_id = $r RETURN d.dsid AS dsid",
            {"r": run_id})
        locator = RowLocator(config.locator_parquet(), config.locator_db(),
                             require_complete=True)
        try:
            bodies = {}
            for row in rows:
                record = locator.fetch(row["dsid"])
                if record:
                    bodies[row["dsid"]] = normalise_content(
                        record.get("content") or "")
        finally:
            locator.close()
        return canonical_map(find_near_duplicates(bodies))
    except Exception as exc:  # noqa: BLE001 - the sweep must still run
        print(f"  near-duplicate detection unavailable ({exc}); "
              "corroboration will not discount copies")
        return {}


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

        # Copies of one another are not independent evidence. Corroboration
        # counts distinct supporting documents, so an edited copy counted twice
        # inflates whichever version happened to be duplicated — and that is one
        # of the four signals deciding which contradictory statement wins.
        #
        # Computed here rather than on the answer path: it is O(n^2) over the
        # ingested working set, which is fine offline and would not be fine in a
        # request. The sweep is where corroboration is recomputed anyway.
        near = near_duplicate_map(client, run_id)
        if near:
            print(f"  {len(near)} document(s) fold into a near-duplicate cluster")

        conflicts, stats = detect_conflicts(
            records, document_order=order, subject_identity=identity,
            near_duplicates=near)

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
        #
        # Through the same function the incremental reconciler uses. Two
        # implementations is how the same-value defect survived being fixed once
        # already: the reconciler stopped pairing claims that agree and this
        # sweep kept doing it, so the next authoritative run would have put every
        # removed edge back.
        pending, edges = conflict_edge_rows(conflicts, run_id)

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
