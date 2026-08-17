"""Keep the conflict graph current as claims arrive.

`CONFLICTS_WITH` edges are what the answer path walks to decide that a fact is
disputed. Writing them once at bootstrap made that decision correct only for the
documents present at bootstrap: on-demand ingestion added claims and stopped, so
a dispute introduced by a document a question had just reached was invisible and
the answer came back singular and confident. Under `bootstrap.sh --fast`, where
nothing is preloaded, *every* dispute was invisible.

So detection runs where the claims are written, once per batch of newly
ingested documents, over the facts those documents actually touch.

The read is one bulk load rather than one lookup per fact. Anchoring per fact
looked cheaper and was not: `MATCH (d:Document)-[:ASSERTS]->(c:Claim) WHERE
c.subject = … AND c.predicate = …` walks the Document label to get at the claim,
and each one cost 3.4 seconds — thirty-six of them per question, two minutes of
a person waiting. Loading every claim in the run costs 3.8 seconds once, and the
grouping is then free in Python. That is the same shape `/api/conflicts` already
used; the mistake was assuming a narrower query is a cheaper one when the cost
tracks the anchor label rather than the rows returned.

The bulk script remains for a full sweep — the two agree because both call
`detect_conflicts`, and edge ids are deterministic, so a claim pair adjudicated
twice converges on one edge rather than two.
"""

from __future__ import annotations

from dataclasses import dataclass

from .conflicts import ClaimRecord, detect_conflicts
from .hydra_client import HydraClient
from .ids import IdRegistry, edge_identity
from .loader import upsert_edges

CLAIM = "Claim"

# Upper bound on the bulk load. The graph holds the enriched working set rather
# than the corpus, so this is generous; it exists so a pathological run cannot
# turn one question into an unbounded read.
MAX_CLAIMS = 8000


@dataclass
class ReconcileReport:
    facts_examined: int = 0
    conflicts_found: int = 0
    edges_written: int = 0
    error: str = ""


def reconcile_conflicts(client: HydraClient, run_id: str, dsids, *,
                        registry: IdRegistry | None = None) -> ReconcileReport:
    """Re-adjudicate every fact these documents have an opinion about.

    `dsids` may be a single dsid or several; passing the whole batch a question
    ingested is preferred, because the read below is paid once either way.
    """
    if isinstance(dsids, str):
        dsids = [dsids]
    targets = set(dsids)
    report = ReconcileReport()
    if not targets:
        return report

    rows = client.bolt_read(
        "MATCH (d:Document)-[:ASSERTS]->(c:Claim)-[:SUPPORTED_BY]->(s:EvidenceSpan) "
        "WHERE c.run_id = $r "
        "RETURN c.id AS claim_id, c.dsid AS dsid, d.source_type AS source_type, "
        "c.subject AS subject, c.predicate AS predicate, c.object AS object, "
        "c.confidence AS confidence, s.quote AS quote, d.timestamp AS timestamp "
        f"LIMIT {MAX_CLAIMS}",
        {"r": run_id})
    if not rows:
        return report

    # Only the facts the new documents actually assert. Everything else in the
    # graph was adjudicated when it arrived and has not changed.
    touched = {(row["subject"], row["predicate"]) for row in rows
               if row["dsid"] in targets and row["subject"] and row["predicate"]}
    report.facts_examined = len(touched)
    if not touched:
        return report

    records = [ClaimRecord(**row) for row in rows
               if (row["subject"], row["predicate"]) in touched]
    if not records:
        return report

    # Same ordering rule as the bulk pass: only documents that state a date take
    # part in recency, because a position a document never claimed would be
    # indistinguishable from evidence once it reached the score.
    timestamps = {r.dsid: r.timestamp for r in records if getattr(r, "timestamp", None)}
    order = sorted(timestamps, key=lambda d: timestamps[d])
    conflicts, _stats = detect_conflicts(records, document_order=order)
    report.conflicts_found = len(conflicts)
    if not conflicts:
        return report

    pending, edges, seen = [], [], set()
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
        upsert_edges(client, "CONFLICTS_WITH", edges,
                     job=f"reconcile:{run_id}:{min(sorted(targets))}:{len(edges)}",
                     source_label=CLAIM, target_label=CLAIM,
                     properties=["predicate", "subject", "decided", "margin",
                                 "run_id"])
        if registry is not None:
            registry.register_many(pending)
    report.edges_written = len(edges)
    return report
