"""The shape of an answer, and the shape of a refusal.

These pin three defects that all had the same character: the system did the
right thing internally and then reported it in a way that read as something
else. An abstention that carries claims and citations is indistinguishable, on
screen, from a supported answer — which is precisely what abstaining exists to
avoid. So the contract is tested, not just the decision behind it.

Nothing here needs a database. The controller's flow is exercised through fakes
so the assertions are about what the contract carries, and a failure points at
the contract rather than at the engine.
"""

from __future__ import annotations

import pytest

from tracegraph.controller import (
    INSUFFICIENT,
    SUPPORTED,
    AnswerController,
    ControllerResult,
)


@pytest.fixture
def controller() -> AnswerController:
    """A controller with no engine behind it.

    `_abstain` and the contract are pure; the graph is only reached by the parts
    these tests do not call.
    """
    return AnswerController.__new__(AnswerController)


def _claims(n: int) -> list[dict]:
    return [
        {"dsid": f"dsid_{i:02d}", "subject": f"s{i}", "predicate": "is",
         "object": f"o{i}", "confidence": 0.9, "quote": f"quote {i}"}
        for i in range(n)
    ]


def test_abstention_carries_no_claims_and_no_citations(controller, monkeypatch):
    """An abstention must not arrive looking like an answer.

    It used to return the claims retrieval had gathered, on the reasoning that
    they were what the system looked at. But `claims` is the field the interface
    renders under "Supporting claims", each with a document id beside it, so an
    abstention reached the screen with citations under it.
    """
    monkeypatch.setattr(controller, "_trace", lambda started: {})

    result = controller._abstain(
        "who owns the runbook?", "nothing supports this", 0.0,
        claims=_claims(12), rejected_spans=[])

    assert result.answerability == INSUFFICIENT
    assert result.confidence == 0.0
    assert result.claims == []
    assert result.document_ids == []

    contract = result.to_contract()
    assert contract["claims"] == []
    assert contract["document_ids"] == []


def test_abstention_still_reports_what_it_examined(controller, monkeypatch):
    """Carrying nothing is not the same as explaining nothing.

    What was looked at and rejected belongs in `examined`, which no caller
    mistakes for support, so a refusal can still show its work.
    """
    monkeypatch.setattr(controller, "_trace", lambda started: {})

    result = controller._abstain(
        "who owns the runbook?", "nothing supports this", 0.0,
        claims=_claims(12), rejected_spans=[])

    assert len(result.examined) == 10, "examined evidence is bounded, not dropped"
    assert result.examined[0]["dsid"] == "dsid_00"
    assert result.to_contract()["examined"] == result.examined


def test_examined_and_claims_are_never_the_same_field(controller, monkeypatch):
    """A supported answer fills `claims`; an abstention fills `examined`.

    If one ever fed the other, the interface could not tell them apart, and the
    distinction the whole contract rests on would be cosmetic.
    """
    monkeypatch.setattr(controller, "_trace", lambda started: {})
    abstention = controller._abstain("q", "r", 0.0, _claims(4), [])

    supported = ControllerResult(
        answer="grounded", document_ids=["dsid_00"], answerability=SUPPORTED,
        confidence=0.8, claims=_claims(4))

    assert abstention.claims == [] and abstention.examined
    assert supported.claims and supported.examined == []


def test_rejected_citations_survive_the_abstention(controller, monkeypatch):
    """A citation the model invented is reported, not silently dropped."""
    monkeypatch.setattr(controller, "_trace", lambda started: {})

    result = controller._abstain(
        "q", "no returned citation survived validation", 0.0,
        claims=_claims(2), rejected_spans=[{"dsid": "dsid_00", "quote": "x"}],
        rejected=["dsid_invented"])

    assert result.rejected_citations == ["dsid_invented"]
    assert len(result.rejected_spans) == 1
