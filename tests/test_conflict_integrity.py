"""Guards for five defects an audit found in the conflict path.

Each was invisible to the suite at the time, and each would have been visible to
a judge: a panel disagreeing with the graph behind it, a winner chosen on a date
nothing else carried, an identity recorded twice and only updated once, an edge
asserting a disagreement between two claims that agree, and a verdict that
depended on which four of eight cited documents were looked at.
"""

from __future__ import annotations

import pytest

from tracegraph.conflicts import (
    MIN_DATED_VERSIONS,
    ClaimRecord,
    detect_conflicts,
)
from tracegraph.controller import AnswerController


def record(dsid, obj, *, claim_id=0, source="gmail", timestamp=None,
           subject="Dana Okafor", predicate="has status"):
    return ClaimRecord(claim_id=claim_id, dsid=dsid, source_type=source,
                       subject=subject, predicate=predicate, object=obj,
                       confidence=0.9, quote=f"{subject} is {obj}",
                       timestamp=timestamp)


# --- recency may not decide from a single dated version ----------------------

def test_one_dated_version_against_undated_ones_decides_nothing():
    """"Dated" is not a synonym for "newer".

    Only 15 of 176 claim-bearing documents in this corpus state a date, so a
    lone dated version was routinely beating undated rivals on recency — it had
    a position in the ordering and they had none. That is an artefact of who
    bothered to write a date, not evidence about which version is current.
    """
    claims = [record("dsid_dated", "closed", claim_id=1, timestamp="2026-05-01"),
              record("dsid_undated_a", "open", claim_id=2),
              record("dsid_undated_b", "blocked", claim_id=3)]
    conflicts, _ = detect_conflicts(claims, document_order=["dsid_dated"])
    assert conflicts, "the disagreement itself must still be reported"
    assert conflicts[0].best is None, (
        "a winner was chosen although only one version carried a date")


def test_two_dated_versions_can_be_separated_by_recency():
    """The rule narrows recency, it does not disable it."""
    claims = [record("dsid_old", "open", claim_id=1, timestamp="2026-01-01"),
              record("dsid_new", "closed", claim_id=2, timestamp="2026-06-01")]
    conflicts, _ = detect_conflicts(
        claims, document_order=["dsid_old", "dsid_new"])
    assert conflicts
    scores = {v.value: v.trust.recency for v in conflicts[0].versions}
    assert len(set(scores.values())) > 1, (
        "two dated versions must be distinguishable on recency")


def test_the_threshold_is_two():
    assert MIN_DATED_VERSIONS == 2


# --- an edge means disagreement, never corroboration -------------------------

def test_claims_supporting_one_value_are_not_edged_together():
    """Two documents agreeing is corroboration, and corroborating claims must
    not be joined by an edge that asserts they conflict.

    The answer reader filtered these out by comparing values, which hid the
    defect rather than fixing it: the graph still held edges asserting something
    false, and anything else reading them believed it.
    """
    conflicts, _ = detect_conflicts(
        [record("dsid_a", "open", claim_id=1),
         record("dsid_b", "open", claim_id=2),      # agrees with the first
         record("dsid_c", "closed", claim_id=3)],
        document_order=["dsid_a", "dsid_b", "dsid_c"])
    assert len(conflicts) == 1

    versions = [sorted({c.claim_id for c in v.claims})
                for v in conflicts[0].versions]
    pairs = {(min(a, b), max(a, b))
             for i, left in enumerate(versions) for right in versions[i + 1:]
             for a in left for b in right}
    assert (1, 2) not in pairs, "two claims on the same value were joined"
    assert (1, 3) in pairs and (2, 3) in pairs, "real disagreements were dropped"


# --- the verdict may not depend on which documents were looked at ------------

def test_every_cited_document_is_checked_for_disputes(monkeypatch):
    """An answer citing documents five through eight must not read `supported`
    while resting on a fact the graph records as disputed."""
    controller = AnswerController.__new__(AnswerController)
    controller.run_id = "run"
    seen: list[str] = []

    def fake_run(operation, cypher, params, hops=1):
        seen.append(params["dsid"])
        return []

    monkeypatch.setattr(controller, "_run", fake_run)
    cited = [{"dsid": f"dsid_{i}", "subject": "s", "predicate": "has status",
              "object": "o", "quote": "q"} for i in range(8)]
    controller.contested(cited)

    assert set(seen) == {f"dsid_{i}" for i in range(8)}, (
        "some cited documents were never checked for disputes")


def test_the_dispute_query_is_ordered(monkeypatch):
    """Unordered LIMIT makes the verdict depend on which slice came back."""
    controller = AnswerController.__new__(AnswerController)
    controller.run_id = "run"
    queries: list[str] = []
    monkeypatch.setattr(
        controller, "_run",
        lambda op, cypher, params, hops=1: queries.append(cypher) or [])
    controller.contested([{"dsid": "d", "subject": "s", "predicate": "has status",
                           "object": "o", "quote": "q"}])
    assert queries and all("ORDER BY" in q for q in queries), (
        "a limited dispute query must be ordered to be reproducible")


# --- the panel must read what reconciliation reads ---------------------------

def test_the_conflicts_endpoint_uses_the_shared_reader():
    """The judge-facing panel had its own capped, identity-blind query, and
    reported a different set of disputes from the graph answers are drawn from."""
    import inspect

    from tracegraph import api

    source = inspect.getsource(api.conflicts)
    assert "load_claims" in source, "the panel is not using the paged reader"
    assert "subject_identity" in source, "the panel is not identity-aware"
    assert "LIMIT 8000" not in source, "the panel still carries a flat cap"
