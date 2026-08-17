"""Conflicts must appear for documents nobody preloaded.

The defect this exists to prevent: `CONFLICTS_WITH` edges were written once by
`scripts/55_conflicts.py` during bootstrap, and on-demand ingestion wrote claims
without ever re-adjudicating. The answer path walks persisted edges, so it could
only ever see disputes that existed at bootstrap. A disagreement introduced by a
document a question had just reached was invisible, and under `--fast`, where
nothing is preloaded, every disagreement was.

Nothing caught it. The stability check could not: no question *required* a
conflicting verdict, so a controller that never detected a conflict at all
passed ten rounds out of ten.

The live test below starts from claims that do not exist yet and drives the real
reconciler against a real engine, because the bug lived in the gap between
components rather than inside one. It writes under the production labels — that
is what the reconciler queries — so it scopes everything to a unique run_id and
removes it afterwards.
"""

from __future__ import annotations

import secrets

import pytest

from tracegraph.conflicts import ClaimRecord, detect_conflicts
from tracegraph.hydra_client import HydraClient
from tracegraph.ids import edge_identity, node_identity
from tracegraph.reconcile import reconcile_conflicts

CONFLICT_COUNT = (
    "MATCH (a:Claim)-[e:CONFLICTS_WITH]->(b:Claim) WHERE e.run_id = $r "
    "RETURN count(*) AS n"
)


def record(dsid: str, obj: str, *, claim_id: int = 0, source: str = "gmail",
           subject: str = "Dana Okafor", predicate: str = "works as",
           timestamp: str = "2026-03-01") -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id, dsid=dsid, source_type=source, subject=subject,
        predicate=predicate, object=obj, confidence=0.9,
        quote=f"{subject} is {obj}", timestamp=timestamp)


# --- the adjudication itself, no database ------------------------------------

def test_two_values_for_one_single_valued_fact_is_a_conflict():
    """The unit the reconciler leans on, pinned apart from the plumbing."""
    conflicts, stats = detect_conflicts(
        [record("dsid_a", "Staff Engineer", claim_id=1),
         record("dsid_b", "Director of Platform", claim_id=2)],
        document_order=["dsid_a", "dsid_b"])
    assert stats["conflicts_found"] == 1
    assert len(conflicts[0].versions) == 2


def test_agreement_is_not_a_conflict():
    """Reconciliation runs on every ingest, so it must not invent disputes."""
    conflicts, stats = detect_conflicts(
        [record("dsid_a", "Staff Engineer", claim_id=1),
         record("dsid_b", "Staff Engineer", claim_id=2)],
        document_order=["dsid_a", "dsid_b"])
    assert stats["conflicts_found"] == 0
    assert conflicts == []


# --- the whole path, against a live engine -----------------------------------


@pytest.fixture
def disputed_graph():
    """Two documents disagreeing about one person's title, and nothing else.

    Written under the real labels because that is what the reconciler queries,
    scoped to a run_id no other session can hold, and torn down by id.
    """
    run_id = f"reconciletest{secrets.token_hex(4)}"
    titles = {"dsid_rc_a": "Staff Engineer", "dsid_rc_b": "Director of Platform"}
    ids: list[int] = []

    with HydraClient() as client:
        client.verify()
        try:
            docs, claims, spans, asserts, supports = [], [], [], [], []
            for i, (dsid, title) in enumerate(titles.items()):
                did = node_identity("Document", f"{run_id}:{dsid}").id
                cid = node_identity("Claim", f"{run_id}:{dsid}:c").id
                sid = node_identity("EvidenceSpan", f"{run_id}:{dsid}:s").id
                ids += [did, cid, sid]
                docs.append({"vertex": did, "dsid": dsid, "run_id": run_id,
                             "source_type": "gmail",
                             "timestamp": f"2026-0{i + 1}-01T00:00:00"})
                claims.append({"vertex": cid, "dsid": dsid, "run_id": run_id,
                               "subject": "Dana Okafor", "predicate": "works as",
                               "object": title, "confidence": 0.9})
                spans.append({"vertex": sid, "dsid": dsid, "run_id": run_id,
                              "quote": f"Dana Okafor is {title}"})
                asserts.append({"src": did, "dst": cid, "run_id": run_id,
                                "eid": edge_identity("ASSERTS", did, cid).id})
                supports.append({"src": cid, "dst": sid, "run_id": run_id,
                                 "eid": edge_identity("SUPPORTED_BY", cid, sid).id})

            from tracegraph.loader import upsert_edges, upsert_nodes
            upsert_nodes(client, "Document", docs, job=f"{run_id}:d",
                         properties=["dsid", "run_id", "source_type", "timestamp"])
            upsert_nodes(client, "Claim", claims, job=f"{run_id}:c",
                         properties=["dsid", "run_id", "subject", "predicate",
                                     "object", "confidence"])
            upsert_nodes(client, "EvidenceSpan", spans, job=f"{run_id}:s",
                         properties=["dsid", "run_id", "quote"])
            upsert_edges(client, "ASSERTS", asserts, job=f"{run_id}:a",
                         source_label="Document", target_label="Claim",
                         properties=["run_id"])
            upsert_edges(client, "SUPPORTED_BY", supports, job=f"{run_id}:sb",
                         source_label="Claim", target_label="EvidenceSpan",
                         properties=["run_id"])

            assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] == 0, (
                "the fixture must start with no conflict edges")
            yield client, run_id
        finally:
            client.bolt_write(
                "UNWIND $rows AS row MATCH (n {id: row.id}) DETACH DELETE n",
                {"rows": [{"id": i} for i in ids]})


@pytest.mark.live
def test_a_dispute_between_two_documents_becomes_a_persisted_edge(disputed_graph):
    """The regression, end to end, starting from claims nothing had judged.

    The persisted edge is what the answer controller walks. Without it the
    disagreement never reaches an answer, however well detection works
    elsewhere.
    """
    client, run_id = disputed_graph
    outcome = reconcile_conflicts(client, run_id, "dsid_rc_a")

    assert outcome.facts_examined == 1
    assert outcome.conflicts_found >= 1, "the disagreement was not detected"
    assert outcome.edges_written >= 1, "the disagreement was not persisted"
    assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] >= 1, (
        "no CONFLICTS_WITH edge reached the graph")


@pytest.mark.live
def test_reconciling_repeatedly_does_not_duplicate_the_edge(disputed_graph):
    """Edge ids are deterministic, so re-adjudication converges.

    It runs on every ingest and the bulk sweep still exists, so the same pair is
    judged repeatedly. Two edges for one disagreement would double-count it
    everywhere it is shown.
    """
    client, run_id = disputed_graph
    reconcile_conflicts(client, run_id, "dsid_rc_a")
    first = client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"]
    assert first >= 1

    reconcile_conflicts(client, run_id, "dsid_rc_a")
    reconcile_conflicts(client, run_id, "dsid_rc_b")
    assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] == first, (
        "the same disagreement was written twice")


@pytest.mark.live
def test_the_controller_finds_what_reconciliation_wrote(disputed_graph):
    """The two halves have to meet, which is exactly where the bug lived.

    Detection wrote edges and the controller read edges, and nothing checked
    that the second could see the first.
    """
    from tracegraph.controller import AnswerController

    client, run_id = disputed_graph
    reconcile_conflicts(client, run_id, "dsid_rc_a")

    controller = AnswerController(client, run_id)
    used = [{"dsid": "dsid_rc_a", "subject": "Dana Okafor",
             "predicate": "works as", "object": "Staff Engineer", "quote": "q"}]
    found = controller.contested(
        used, asserted_in="Dana Okafor is a Staff Engineer.")

    assert found, "the controller could not see the edge reconciliation wrote"
    assert found[0]["rival_value"] == "Director of Platform"
    assert found[0]["rival_dsid"] == "dsid_rc_b"
