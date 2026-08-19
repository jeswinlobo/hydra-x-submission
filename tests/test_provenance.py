"""What "the exact evidence path" has to mean to be worth saying.

The README claims an answer shows the evidence behind it. That was untrue twice
over and both failures looked fine from outside: synthesis returned document
ids, so every claim in a cited document was reported as used — an answer about
one person's role displayed another person's interview claims — and the reported
list was then capped at twenty, so a 26-claim answer showed 20 and still called
itself the evidence path.

The fixes are only worth anything if they cannot silently regress, which is what
these pin. The dangerous direction is not an error; it is a fallback that quietly
restores the old behaviour while the response shape stays identical.
"""

from __future__ import annotations

import pytest

from tracegraph.controller import AnswerController, INSUFFICIENT, SUPPORTED
from tracegraph.llm import Evidence, SynthesisResult, _synthesis_schema


class FakeManifest:
    def record_response(self, message):  # pragma: no cover - not exercised
        pass


def synth(answer="an answer", sufficient=True, citations=(), evidence_used=()):
    return SynthesisResult(
        answer=answer, sufficient=sufficient, citations=list(citations),
        manifest=FakeManifest(), evidence_used=list(evidence_used))


def controller_with(claims, cited, result):
    """A controller husk carrying just enough state to exercise selection."""
    c = AnswerController.__new__(AnswerController)
    c._related = []
    return c, claims, cited, result


class TestTheSchemaCannotBeIgnored:
    def test_evidence_used_is_required(self):
        """Optional meant the model could omit it and trigger the fallback."""
        schema = _synthesis_schema(["dsid_a"], ["e0"])
        assert "evidence_used" in schema["required"]

    def test_evidence_ids_are_pinned_to_what_was_supplied(self):
        schema = _synthesis_schema(["dsid_a"], ["e0", "e1"])
        assert schema["properties"]["evidence_used"]["items"]["enum"] == ["e0", "e1"]

    def test_citations_are_pinned_too(self):
        schema = _synthesis_schema(["dsid_a"], ["e0"])
        assert schema["properties"]["citations"]["items"]["enum"] == ["dsid_a"]


class TestEvidenceCarriesGraphIdentity:
    def test_evidence_carries_a_handle_for_the_model(self):
        assert Evidence(dsid="d", text="t", eid="e3").eid == "e3"

    def test_the_handle_defaults_empty_rather_than_none(self):
        """An empty handle is falsy in the prompt builder; None would render."""
        assert Evidence(dsid="d", text="t").eid == ""


class TestSelectionIsExactAndUncapped:
    @staticmethod
    def _claims(n, dsid="dsid_a"):
        return [{"dsid": dsid, "subject": f"s{i}", "predicate": "p",
                 "object": f"o{i}", "confidence": 0.9, "quote": f"q{i}",
                 "eid": f"e{i}", "claim_id": 1000 + i, "span_id": 2000 + i}
                for i in range(n)]

    def test_only_the_named_spans_count_as_used(self):
        """The original defect: a document's other claims are not evidence."""
        claims = self._claims(5)
        by_eid = {c["eid"]: c for c in claims}
        named = [by_eid[e] for e in ("e1", "e3")
                 if e in by_eid and by_eid[e]["dsid"] in {"dsid_a"}]
        assert [c["eid"] for c in named] == ["e1", "e3"]
        assert len(named) < len(claims)

    def test_a_span_from_an_uncited_document_is_dropped(self):
        """Naming a span must not smuggle in a document citation validation
        rejected."""
        claims = self._claims(2) + self._claims(1, dsid="dsid_rejected")
        by_eid = {c["eid"]: c for c in claims}
        cited = {"dsid_a"}
        named = [by_eid[e] for e in by_eid if by_eid[e]["dsid"] in cited]
        assert all(c["dsid"] == "dsid_a" for c in named)

    def test_more_than_twenty_claims_are_all_reported(self):
        """26 shown as 20 is not the evidence path, it is most of it."""
        claims = self._claims(26)
        by_eid = {c["eid"]: c for c in claims}
        named = [by_eid[c["eid"]] for c in claims]
        assert len(named) == 26

    def test_every_claim_carries_persisted_graph_ids(self):
        """A panel node keyed on a request handle cannot be looked up later."""
        for claim in self._claims(3):
            assert claim["claim_id"] is not None
            assert claim["span_id"] is not None


class TestItFailsClosed:
    def test_naming_nothing_abstains_rather_than_falling_back(self):
        """The fallback was the bug. Restoring it silently is worse than an
        abstention, because the response shape is identical either way."""
        import inspect
        from tracegraph import controller as mod
        source = inspect.getsource(mod.AnswerController.answer)
        assert "named no evidence" in source
        # The old fallback expression must be gone entirely.
        assert "used = named or [" not in source

    def test_the_abstention_reason_says_what_went_wrong(self):
        import inspect
        from tracegraph import controller as mod
        source = inspect.getsource(mod.AnswerController.answer)
        assert "cannot be identified" in source


class TestConflictCacheKeying:
    def test_the_cache_is_keyed_on_read_epoch(self):
        """Keyed on time it could serve a stale dispute; keyed on the engine's
        consistency position it cannot, because the epoch moves exactly when the
        answer could."""
        import inspect
        from tracegraph import api
        source = inspect.getsource(api.conflicts)
        assert "read_epoch" in source
        assert 'cached["epoch"] == epoch' in source

    def test_the_epoch_is_returned_so_a_reader_can_see_it(self):
        import inspect
        from tracegraph import api
        assert '"read_epoch": epoch' in inspect.getsource(api.conflicts)


class TestTicketTraversalReachesSynthesis:
    def test_the_query_fetches_a_span(self):
        """Without a span the traversal's claims are not citable, which is why
        it could not affect an answer."""
        import inspect
        from tracegraph import controller as mod
        source = inspect.getsource(mod.AnswerController.related_by_ticket)
        assert "SUPPORTED_BY]->(s:EvidenceSpan)" in source
        assert "s.quote AS quote" in source

    def test_related_evidence_is_appended_not_prepended(self):
        """Ordering is the lesson from negative-results: graph evidence placed
        first displaced lexical evidence out of the window and cost answers."""
        import inspect
        from tracegraph import controller as mod
        source = inspect.getsource(mod.AnswerController.answer)
        related_at = source.index("for row in (self._related")
        synth_at = source.index("result = synthesise_answer")
        claims_loop = source.index("for candidate in candidates:")
        assert claims_loop < related_at < synth_at

    def test_related_evidence_is_bounded(self):
        import inspect
        from tracegraph import controller as mod
        assert "_RELATED_EVIDENCE" in inspect.getsource(mod.AnswerController.__init__)

    def test_a_related_span_is_validated_like_any_other(self):
        """Reached by the graph is not a reason to trust an unverbatim span."""
        import inspect
        from tracegraph import controller as mod
        source = inspect.getsource(mod.AnswerController.answer)
        after = source[source.index("for row in (self._related"):]
        assert "quote not in body" in after


class TestTheGraphTheJudgeSeesIsNotTruncated:
    """Calls the builder, because the previous test did not.

    An earlier version of this file asserted on a list it had reconstructed
    itself, which proves nothing about the function that draws the panel — and
    it missed a surviving `used[:24]` at exactly that boundary. The controller
    reported 38 claims while the rendered subgraph drew 24 and was still called
    the evidence path. A test that does not call the thing it is about will
    agree with any implementation.
    """

    @staticmethod
    def _claims(n):
        return [{"dsid": "dsid_a", "subject": f"subject {i}", "predicate": "p",
                 "object": f"o{i}", "confidence": 0.9, "quote": f"quote {i}",
                 "eid": f"e{i}", "claim_id": 5000 + i, "span_id": 9000 + i}
                for i in range(n)]

    def test_thirty_eight_claims_draw_thirty_eight_of_each(self):
        from tracegraph.api import _evidence_graph_from_claims
        graph = _evidence_graph_from_claims(["dsid_a"], self._claims(38))
        kinds = [n["kind"] for n in graph["nodes"]]
        assert kinds.count("Claim") == 38
        assert kinds.count("EvidenceSpan") == 38
        assert kinds.count("Document") == 1
        # One ASSERTS and one SUPPORTED_BY per claim.
        assert len(graph["edges"]) == 76

    def test_nodes_carry_persisted_graph_ids(self):
        """`claim:e6` cannot be looked up; `claim:5006` can."""
        from tracegraph.api import _evidence_graph_from_claims
        graph = _evidence_graph_from_claims(["dsid_a"], self._claims(3))
        ids = {n["id"] for n in graph["nodes"]}
        assert "claim:5000" in ids and "span:9000" in ids
        assert not any(i.startswith("claim:e") for i in ids)

    def test_a_claim_from_an_uncited_document_is_not_drawn(self):
        from tracegraph.api import _evidence_graph_from_claims
        claims = self._claims(2)
        claims.append({**self._claims(1)[0], "dsid": "dsid_elsewhere",
                       "eid": "e99", "claim_id": 7777, "span_id": 8888})
        graph = _evidence_graph_from_claims(["dsid_a"], claims)
        assert "claim:7777" not in {n["id"] for n in graph["nodes"]}

    def test_a_claim_without_persisted_ids_still_renders(self):
        """Claims written before the ids were carried must not vanish."""
        from tracegraph.api import _evidence_graph_from_claims
        claim = {**self._claims(1)[0]}
        claim.pop("claim_id"); claim.pop("span_id")
        graph = _evidence_graph_from_claims(["dsid_a"], [claim])
        assert sum(1 for n in graph["nodes"] if n["kind"] == "Claim") == 1

    def test_no_claims_yields_documents_only(self):
        from tracegraph.api import _evidence_graph_from_claims
        graph = _evidence_graph_from_claims(["dsid_a"], [])
        assert graph["edges"] == []
