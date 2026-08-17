"""An answer that rests on a contested fact must not read as settled.

The track brief names four things a question can need: a lookup, multi-hop
reasoning, conflict resolution, and knowing when the answer is absent. Three
were served. The fourth had `CONFLICTING` defined, `alternatives` in the answer
contract, and `CONFLICTS_WITH` edges in the graph — and the answer path consulted
none of them, so a question about a disputed fact came back confident and
singular.

These pin the contract. `tests/test_hydra_contract.py` is what proves the
traversal behind it runs on a real engine.
"""

from __future__ import annotations

import pytest

from tracegraph.controller import (
    CONFLICTING,
    INSUFFICIENT,
    SUPPORTED,
    AnswerController,
    ControllerResult,
    _states,
)


@pytest.fixture
def controller() -> AnswerController:
    return AnswerController.__new__(AnswerController)


def contested_row(rival: str, cited: str = "Hiring Manager, Inference Runtime"):
    return {
        "subject": "Grace O'Connor", "predicate": "works as",
        "cited_value": cited, "rival_value": rival,
        "rival_dsid": "dsid_rival", "decided": False, "margin": 0.0999,
    }


def test_a_contested_answer_is_labelled_conflicting_not_supported():
    result = ControllerResult(
        answer="She is the hiring manager.", document_ids=["dsid_a"],
        answerability=CONFLICTING, confidence=0.6,
        claims=[{"dsid": "dsid_a"}],
        alternatives=[contested_row("Director, Talent Strategy")])
    assert result.answerability == CONFLICTING
    assert result.answerability != SUPPORTED
    assert result.to_contract()["alternatives"], "the rival version must travel"


def test_the_contract_carries_both_versions_and_where_each_came_from():
    """A reader has to be able to go and check the disagreement themselves."""
    alt = contested_row("Manager, Security & Compliance")
    contract = ControllerResult(
        answer="x", document_ids=["dsid_a"], answerability=CONFLICTING,
        confidence=0.6, alternatives=[alt]).to_contract()
    carried = contract["alternatives"][0]
    assert carried["cited_value"] != carried["rival_value"]
    assert carried["rival_dsid"], "the rival needs a document id to be checkable"
    assert "decided" in carried, "whether the graph could separate them must show"


USED = [{"dsid": "dsid_a", "subject": "Grace O'Connor", "predicate": "works as",
         "object": "Hiring Manager", "quote": "q"}]


def test_identical_values_are_not_a_conflict(controller, monkeypatch):
    """Two documents agreeing is not a disagreement.

    The edge exists between claims, and two claims can carry the same object.
    Reporting that as contested would cry wolf on every corroboration.
    """
    rows = [{"subject": "s", "predicate": "p", "cited_value": "same",
             "rival_value": "same", "rival_dsid": "dsid_b",
             "decided": False, "margin": 0.0}]
    monkeypatch.setattr(controller, "_run", lambda *a, **k: rows)
    controller.run_id = "run"
    assert controller.contested(USED) == []


def test_a_conflict_is_reported_once_however_it_is_reached(controller, monkeypatch):
    """The edge is written once but walked from both ends."""
    row = {"subject": "Grace O'Connor", "predicate": "works as",
           "cited_value": "A", "rival_value": "B", "rival_dsid": "dsid_b",
           "decided": False, "margin": 0.1}
    monkeypatch.setattr(controller, "_run", lambda *a, **k: [row])
    controller.run_id = "run"
    # Two directions x two claims = four hits on the same disagreement.
    both = USED + [{**USED[0], "dsid": "dsid_b"}]
    assert len(controller.contested(both)) == 1


def test_a_value_the_answer_never_states_is_not_flagged(controller, monkeypatch):
    """The precision filter, and the reason it exists.

    Every claim extracted from a cited document is handed to the model, not
    just the ones it used. A document about SOC 2 commitments also carries job
    titles; those being contested elsewhere must not mark a SOC 2 answer
    conflicting over a fact it never asserted.
    """
    # The row has to name a fact the answer actually used, or the narrowing
    # step drops it before the prose is even consulted.
    row = {"subject": "Grace O'Connor", "predicate": "works as",
           "cited_value": "Hiring Manager, Inference Runtime",
           "rival_value": "Director, Talent Strategy",
           "rival_dsid": "dsid_b", "decided": False, "margin": 0.0}
    monkeypatch.setattr(controller, "_run", lambda *a, **k: [row])
    controller.run_id = "run"

    unrelated = "Redwood committed to a SOC 2 Type II report and quarterly evidence."
    assert controller.contested(USED, asserted_in=unrelated) == []

    asserting = "Grace O'Connor is the Hiring Manager for Inference Runtime."
    assert len(controller.contested(USED, asserted_in=asserting)) == 1


def test_a_conflict_about_a_fact_the_answer_did_not_use_is_dropped(
        controller, monkeypatch):
    """The narrowing step, independent of the prose filter.

    Every claim extracted from a cited document is walked, but only the ones the
    answer had as evidence may raise a dispute.
    """
    row = {"subject": "Ben Carter", "predicate": "has title",
           "cited_value": "Senior Account Executive",
           "rival_value": "Principal Serving Engineer",
           "rival_dsid": "dsid_b", "decided": False, "margin": 0.0}
    monkeypatch.setattr(controller, "_run", lambda *a, **k: [row])
    controller.run_id = "run"
    # USED is about Grace O'Connor, so Ben Carter's disputed title is not ours.
    assert controller.contested(USED) == []


@pytest.mark.parametrize("answer,value,expected", [
    ("Marissa Cole is Director of Trust & Security at Redwood Inference.",
     "Director, Trust & Security, Redwood Inference", True),
    ("Redwood committed to a SOC 2 Type II report.",
     "Senior Account Executive, Redwood Inference", False),
    ("", "anything", False),
    ("some answer", "", False),
    # A single word present verbatim really is asserted.
    ("the runtime shipped", "runtime", True),
    # But sharing one token out of several is a coincidence, not an assertion.
    ("the runtime shipped on Tuesday", "Principal Runtime Engineer, Redwood", False),
])
def test_states_requires_the_answer_to_carry_the_value(answer, value, expected):
    """Reformatting is tolerated; a one-word coincidence is not."""
    assert _states(answer, value) is expected


def test_an_abstention_is_never_relabelled_conflicting():
    """Absent evidence and disputed evidence are different answers."""
    result = ControllerResult(
        answer="no", document_ids=[], answerability=INSUFFICIENT, confidence=0.0)
    assert result.answerability == INSUFFICIENT
    assert result.alternatives == []
