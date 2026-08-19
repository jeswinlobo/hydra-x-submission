"""Tests for the guarantee every citation in this system rests on.

`llm.validate_spans` is the only thing standing between a model's assertion and
a claim written to the graph, and `llm.py` calls it "the invariant the whole
submission rests on". It had no tests, which meant the README's claim — that a
span altering a single word of a real sentence is refused — was an assertion
about code rather than a property anyone had checked.

The cases below are written against what the pipeline actually does with the
result: offsets go straight onto EvidenceSpan nodes without re-searching the
document, so an offset that is merely *plausible* is a silent corruption; and
rejection has to be per-claim, because extraction returns a batch and one bad
span must not discard the good ones beside it.
"""

from __future__ import annotations

import pytest

from tracegraph.llm import validate_spans

DOC = (
    "Grace O'Connor is the Hiring Manager for Inference Runtime.\n"
    "The rollout was paused after P95 latency regressed to 1.8s.\n"
    "Priya Sharma approved the SOC 2 evidence request on 3 March.\n"
)


def claim(**over):
    """A claim that validates, so each test varies exactly one thing."""
    base = {
        "subject": "Grace O'Connor",
        "predicate": "holds_title",
        "object": "Hiring Manager",
        "object_type": "title",
        "evidence_span": "Grace O'Connor is the Hiring Manager for Inference Runtime.",
        "confidence": 0.9,
    }
    base.update(over)
    return base


class TestVerbatimIsTheWholeTest:
    def test_an_exact_span_is_accepted(self):
        accepted, rejected = validate_spans([claim()], DOC, doc_id="d1")
        assert len(accepted) == 1
        assert not rejected
        assert accepted[0].evidence_span in DOC

    def test_altering_a_single_word_is_refused(self):
        """The README's headline claim about evidence discipline, as a test.

        `Senior Hiring Manager` is a plausible paraphrase of a real sentence and
        is exactly the failure this function exists to catch: the claim would
        read as grounded, and the span would not be in the document.
        """
        bad = claim(
            evidence_span="Grace O'Connor is the Senior Hiring Manager for "
                          "Inference Runtime."
        )
        accepted, rejected = validate_spans([bad], DOC, doc_id="d1")
        assert not accepted
        assert [r.reason for r in rejected] == ["span_not_verbatim"]

    def test_altering_only_punctuation_is_refused(self):
        """Verbatim means verbatim; a smart quote is not a repair opportunity."""
        bad = claim(
            evidence_span="Grace O’Connor is the Hiring Manager for "
                          "Inference Runtime."
        )
        accepted, rejected = validate_spans([bad], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "span_not_verbatim"

    def test_case_is_not_normalised_away(self):
        bad = claim(evidence_span="grace o'connor is the hiring manager")
        accepted, rejected = validate_spans([bad], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "span_not_verbatim"

    def test_collapsed_whitespace_is_refused(self):
        """A span joining two lines with a space is not what the document says."""
        bad = claim(
            evidence_span="Inference Runtime. The rollout was paused"
        )
        accepted, rejected = validate_spans([bad], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "span_not_verbatim"

    def test_a_span_crossing_a_line_break_is_accepted_if_it_is_real(self):
        """The rule is substring, not sentence — a real multi-line span stands."""
        good = claim(
            evidence_span="Inference Runtime.\nThe rollout was paused"
        )
        accepted, rejected = validate_spans([good], DOC, doc_id="d1")
        assert len(accepted) == 1
        assert not rejected


class TestOffsets:
    def test_offsets_index_the_span_they_report(self):
        """EvidenceSpan nodes trust these offsets without re-searching."""
        accepted, _ = validate_spans([claim()], DOC, doc_id="d1")
        span = accepted[0]
        assert DOC[span.span_start:span.span_end] == span.evidence_span

    def test_offsets_are_the_first_occurrence(self):
        doc = "paused. paused."
        accepted, _ = validate_spans(
            [claim(evidence_span="paused.")], doc, doc_id="d1")
        assert (accepted[0].span_start, accepted[0].span_end) == (0, 7)
        assert doc[0:7] == "paused."


class TestMalformedClaimsAreRejectedNotRepaired:
    @pytest.mark.parametrize("field", [
        "subject", "predicate", "object", "object_type", "evidence_span",
        "confidence",
    ])
    def test_a_missing_required_field_is_malformed(self, field):
        bad = claim()
        del bad[field]
        accepted, rejected = validate_spans([bad], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "malformed"
        assert field in rejected[0].detail

    def test_an_empty_span_is_rejected_before_it_can_match_everything(self):
        """`"" in doc` is true for every document; this must not be a pass."""
        accepted, rejected = validate_spans(
            [claim(evidence_span="")], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "empty_span"

    def test_a_non_string_span_is_rejected(self):
        accepted, rejected = validate_spans(
            [claim(evidence_span=None)], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "empty_span"

    def test_a_non_numeric_confidence_is_malformed(self):
        accepted, rejected = validate_spans(
            [claim(confidence="very")], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "malformed"

    @pytest.mark.parametrize("value", [-0.1, 1.1, 42])
    def test_confidence_outside_zero_to_one_is_rejected(self, value):
        accepted, rejected = validate_spans(
            [claim(confidence=value)], DOC, doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "confidence_out_of_range"

    def test_a_numeric_string_confidence_is_accepted(self):
        """Structured output has returned `"0.9"`; that is coercion, not repair."""
        accepted, rejected = validate_spans(
            [claim(confidence="0.9")], DOC, doc_id="d1")
        assert len(accepted) == 1
        assert accepted[0].confidence == pytest.approx(0.9)


class TestBatchBehaviour:
    def test_one_bad_claim_does_not_discard_the_good_ones(self):
        """Extraction returns a batch; rejection is per-claim by design."""
        good = claim()
        other = claim(
            subject="Priya Sharma", predicate="approved",
            object="SOC 2 evidence request",
            evidence_span="Priya Sharma approved the SOC 2 evidence request",
        )
        bad = claim(evidence_span="Grace O'Connor is the Director of Talent.")
        accepted, rejected = validate_spans([good, bad, other], DOC, doc_id="d1")
        assert len(accepted) == 2
        assert len(rejected) == 1
        assert rejected[0].reason == "span_not_verbatim"

    def test_every_rejection_carries_the_claim_that_caused_it(self):
        """The pilot batch reported 62 rejections; each has to be inspectable."""
        bad = claim(evidence_span="not in this document at all")
        _, rejected = validate_spans([bad], DOC, doc_id="d7")
        assert rejected[0].doc_id == "d7"
        assert rejected[0].claim["subject"] == "Grace O'Connor"
        assert "not in this document" in rejected[0].detail

    def test_the_input_mapping_is_not_mutated(self):
        """Callers log the raw claim after validation; it must be unchanged."""
        original = claim()
        snapshot = dict(original)
        validate_spans([original], DOC, doc_id="d1")
        assert original == snapshot

    def test_an_empty_batch_is_not_an_error(self):
        assert validate_spans([], DOC, doc_id="d1") == ([], [])

    def test_nothing_validates_against_an_empty_document(self):
        accepted, rejected = validate_spans([claim()], "", doc_id="d1")
        assert not accepted
        assert rejected[0].reason == "span_not_verbatim"
