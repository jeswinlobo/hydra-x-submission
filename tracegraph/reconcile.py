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

import logging

from .conflicts import ClaimRecord, detect_conflicts, group_key
from .hydra_client import HydraClient
from .ids import IdRegistry, edge_identity
from .loader import upsert_edges
logger = logging.getLogger(__name__)

CLAIM = "Claim"

# Claims are read a page at a time rather than under one big LIMIT.
#
# A flat cap is a correctness risk disguised as a safety valve: the moment the
# graph holds more claims than the cap, adjudication silently judges each
# dispute against a subset, and which subset depends on whatever order the
# engine happened to return. Paging with a stable `ORDER BY c.id` reads all of
# them, so the picture does not narrow as the working set grows.
CLAIM_PAGE = 4000
RESOLUTION_PAGE = 8000

# A ceiling on total pages, so a runaway read cannot hang a question forever.
# Reaching it raises: adjudication over a silently truncated set produces edges
# that look authoritative and are not, and a slow read is recoverable where a
# wrong graph is not.
MAX_PAGES = 200


class IncompleteRead(RuntimeError):
    """A paged read hit its ceiling before returning everything."""



def _read_all(client: HydraClient, cypher: str, params: dict, page: int,
              order_by: str) -> list[dict]:
    """Read every row, without paying for ordering unless paging is needed.

    One unordered read first, asking for one row more than a page. If fewer
    come back than that, the whole set is in hand and no order was required —
    which is the common case, and `ORDER BY` roughly doubles the cost of this
    traversal.

    Only when the set genuinely exceeds a page does it fall back to ordered
    paging, and then the order matters: `ORDER BY` before `SKIP`/`LIMIT` is what
    makes pages disjoint. A flat cap instead of either would be a correctness
    risk disguised as a safety valve — past the cap, every dispute is silently
    judged against an arbitrary subset.
    """
    probe = client.bolt_read(f"{cypher} LIMIT {page + 1}", params)
    if len(probe) <= page:
        return probe

    out: list[dict] = []
    for index in range(MAX_PAGES):
        rows = client.bolt_read(
            f"{cypher} {order_by} SKIP {index * page} LIMIT {page}", params)
        out.extend(rows)
        if len(rows) < page:
            return out
    raise IncompleteRead(
        f"read {MAX_PAGES} pages of {page} rows without reaching the end; "
        "adjudicating over a truncated set would write edges that look "
        "authoritative and are not")


def load_subject_identity(client: HydraClient, run_id: str) -> dict:
    """Map `(dsid, casefolded surface)` to the entity the resolver chose.

    This is what lets adjudication ask "is this the same person?" rather than
    "is this the same name?".

    Read off the mention rather than by walking `RESOLVES_TO` to the entity. The
    walk is anchored on `Mention`, of which there are thousands, and cost 7.6
    seconds against 0.7 for reading the mentions alone — paid inside somebody's
    question. The mention already records *how* it resolved, so recording *what*
    it resolved to is the same kind of fact in the same place.
    """
    rows = _read_all(
        client,
        "MATCH (m:Mention) WHERE m.run_id = $r AND m.status = 'resolved' "
        "RETURN m.dsid AS dsid, m.normalised AS surface, m.entity AS entity_id",
        {"r": run_id}, RESOLUTION_PAGE, "ORDER BY m.id")
    return {(row["dsid"], row["surface"]): row["entity_id"]
            for row in rows
            if row["dsid"] and row["surface"] and row["entity_id"]}


def load_claims(client: HydraClient, run_id: str) -> list[dict]:
    """Every claim in the run, with the document and span each needs."""
    return _read_all(
        client,
        "MATCH (d:Document)-[:ASSERTS]->(c:Claim)-[:SUPPORTED_BY]->(s:EvidenceSpan) "
        "WHERE c.run_id = $r "
        "RETURN c.id AS claim_id, c.dsid AS dsid, d.source_type AS source_type, "
        "c.subject AS subject, c.predicate AS predicate, c.object AS object, "
        "c.confidence AS confidence, s.quote AS quote, d.timestamp AS timestamp",
        {"r": run_id}, CLAIM_PAGE, "ORDER BY c.id")


@dataclass
class ReconcileReport:
    facts_examined: int = 0
    conflicts_found: int = 0
    edges_written: int = 0
    truncated: bool = False
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

    rows = load_claims(client, run_id)
    if not rows:
        return report

    # Only the facts the new documents actually assert — everything else was
    # adjudicated when it arrived. Keyed by `group_key`, which is the same
    # function adjudication uses, so selection cannot disagree with it about
    # what counts as the same fact. Both previous defects were exactly that
    # disagreement: once over predicate spelling, once over subject spelling.
    identity = load_subject_identity(client, run_id)
    keys = {row["claim_id"]: group_key(row["dsid"], row["subject"],
                                       row["predicate"], identity)
            for row in rows}
    touched = {keys[row["claim_id"]] for row in rows
               if row["dsid"] in targets and keys[row["claim_id"]] is not None}
    report.facts_examined = len(touched)
    if not touched:
        return report

    records = [ClaimRecord(**row) for row in rows
               if keys[row["claim_id"]] in touched]
    if not records:
        return report

    # Same ordering rule as the bulk pass: only documents that state a date take
    # part in recency, because a position a document never claimed would be
    # indistinguishable from evidence once it reached the score.
    timestamps = {r.dsid: r.timestamp for r in records if getattr(r, "timestamp", None)}
    order = sorted(timestamps, key=lambda d: timestamps[d])
    conflicts, _stats = detect_conflicts(
        records, document_order=order, subject_identity=identity)
    report.conflicts_found = len(conflicts)
    if not conflicts:
        return report

    pending, edges = conflict_edge_rows(conflicts, run_id)

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


def conflict_edge_rows(conflicts, run_id: str) -> tuple[list, list[dict]]:
    """The rows and identities for a set of contested facts.

    One implementation, called by both writers. Having two was how the
    same-value defect survived being fixed: the incremental reconciler stopped
    pairing claims that agree, and the authoritative sweep kept doing it, so the
    next full sweep would have recreated every edge the fix removed.

    An edge means "these two claims disagree", so it is drawn only *between*
    value groups, never within one. Two documents asserting the same value
    corroborate each other; joining them with a conflict edge asserts something
    false, and the answer reader's habit of filtering those by comparing values
    hid that rather than fixing it — the graph still held the false edge and
    anything else reading it believed it.
    """
    pending, edges, seen = [], [], set()
    for conflict in conflicts:
        versions = [sorted({c.claim_id for c in version.claims})
                    for version in conflict.versions]
        for index, left_version in enumerate(versions):
            for right_version in versions[index + 1:]:
                for a in left_version:
                    for b in right_version:
                        left, right = (a, b) if a <= b else (b, a)
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
    return pending, edges


def prune_superseded(client: HydraClient, run_id: str,
                     justified: set[tuple[int, int]]) -> int:
    """Remove conflict edges a full sweep no longer produces.

    Only a sweep may call this, because only a sweep has looked at everything.
    The incremental pass sees the facts one batch touched and would read every
    other edge as unjustified.

    It matters because adjudication rules change. When grouping moved from the
    subject's name to the resolved identity, thirty-one edges joining two
    different people stopped being produced — and stayed in the graph, and kept
    being reported, because writing is a MERGE and nothing ever retracted them.

    Edge ids are recomputed from the endpoints rather than read back: the engine
    will not return `r.id` for a relationship. See docs/engine-notes.md.
    """
    rows = _read_all(
        client,
        "MATCH (a:Claim)-[e:CONFLICTS_WITH]->(b:Claim) WHERE e.run_id = $r "
        "RETURN a.id AS src, b.id AS dst, e.predicate AS predicate",
        {"r": run_id}, CLAIM_PAGE, "ORDER BY a.id")
    stale = [edge_identity("CONFLICTS_WITH", row["src"], row["dst"],
                           row["predicate"] or "").id
             for row in rows if (row["src"], row["dst"]) not in justified]
    if not stale:
        return 0
    for start in range(0, len(stale), 400):
        client.bolt_write(
            "UNWIND $rows AS row MATCH ()-[e:CONFLICTS_WITH {id: row.eid}]->() DELETE e",
            {"rows": [{"eid": int(e)} for e in stale[start:start + 400]]})
    return len(stale)
