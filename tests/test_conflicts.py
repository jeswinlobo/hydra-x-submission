"""Ontology alignment and conflict detection.

The cases that matter here are the ones where a naive implementation reports a
conflict that is not one, or fails to report one that is.
"""

from __future__ import annotations

from tracegraph.conflicts import ClaimRecord, detect_conflicts
from tracegraph.ontology import MULTI, SINGLE, align, normalise_object, source_authority


def record(subject, predicate, obj, dsid, source="gmail", quote="q"):
    return ClaimRecord(
        claim_id=abs(hash((subject, predicate, obj, dsid))) % (10**9),
        dsid=dsid, source_type=source, subject=subject, predicate=predicate,
        object=obj, confidence=0.9, quote=quote,
    )


class TestAlignment:
    def test_synonyms_collapse_to_one_predicate(self):
        names = {align(raw).predicate.name
                 for raw in ("has title", "job title", "has job title",
                             "has role", "works as")}
        assert names == {"holds_title"}

    def test_cardinality_separates_conflictable_relations(self):
        assert align("has title").predicate.cardinality == SINGLE
        assert align("includes").predicate.cardinality == MULTI
        assert align("has email").predicate.cardinality == MULTI

    def test_unknown_predicate_is_declined_not_guessed(self):
        alignment = align("frobnicates the widget")
        assert not alignment.aligned
        assert alignment.method == "unmapped"

    def test_authority_ranks_systems_of_record(self):
        status = align("has status").predicate
        assert source_authority(status, "jira") > source_authority(status, "slack")

    def test_object_normalisation_is_shallow(self):
        assert normalise_object("Redwood Inference") == normalise_object("Redwood")
        # It must not collapse genuinely different values.
        assert normalise_object("Account Executive") != normalise_object("Sales Engineer")


class TestConflictDetection:
    def test_multi_valued_predicate_is_not_a_conflict(self):
        """Three things a runbook includes are three facts, not a contradiction."""
        records = [
            record("rollback criteria", "includes", "5xx > 1%", "d1"),
            record("rollback criteria", "includes", "p95 > 2x", "d2"),
            record("rollback criteria", "includes", "duplicate sessions", "d3"),
        ]
        conflicts, _ = detect_conflicts(records)
        assert conflicts == []

    def test_multiple_emails_are_not_a_conflict(self):
        records = [
            record("Karthik Iyer", "has email", "k@redwood.ai", "d1"),
            record("Karthik Iyer", "has email", "k@redwood.com", "d2"),
        ]
        conflicts, _ = detect_conflicts(records)
        assert conflicts == []

    def test_single_valued_disagreement_is_a_conflict(self):
        records = [
            record("Laura Bennett", "has title", "Sr. Counsel", "d1"),
            record("Laura Bennett", "job title", "Head of Strategic Accounts", "d2"),
        ]
        conflicts, stats = detect_conflicts(records, document_order=["d1", "d2"])
        assert len(conflicts) == 1
        assert stats["conflicts_found"] == 1
        assert len(conflicts[0].versions) == 2

    def test_no_losing_version_is_discarded(self):
        """A conflict answer has to carry every supported version."""
        records = [
            record("X", "has status", "open", "d1"),
            record("X", "has status", "closed", "d2"),
        ]
        conflicts, _ = detect_conflicts(records, document_order=["d1", "d2"])
        values = {v.display for v in conflicts[0].versions}
        assert values == {"open", "closed"}

    def test_recency_decides_a_mutable_relation(self):
        """A later job title supersedes an earlier one."""
        records = [
            record("Ana", "has title", "Engineer", "d1"),
            record("Ana", "has title", "Staff Engineer", "d2"),
        ]
        conflicts, _ = detect_conflicts(records, document_order=["d1", "d2"])
        conflict = conflicts[0]
        assert conflict.decided
        assert conflict.best.display == "Staff Engineer"
        assert "superseded" in conflict.reason

    def test_authority_outranks_recency_where_a_system_of_record_exists(self):
        """A ticket's own tracker beats a later mention in chat."""
        records = [
            record("BILL-1", "has status", "closed", "d1", source="jira"),
            record("BILL-1", "has status", "open", "d2", source="slack"),
        ]
        conflicts, _ = detect_conflicts(records, document_order=["d1", "d2"])
        conflict = conflicts[0]
        assert conflict.decided
        assert conflict.best.display == "closed"

    def test_a_tie_is_left_undecided(self):
        """Manufacturing a winner is worse than reporting the disagreement."""
        records = [
            record("Y", "has status", "open", "d1"),
            record("Y", "has status", "blocked", "d2"),
        ]
        # No ordering, so recency is neutral and nothing separates the versions.
        conflicts, _ = detect_conflicts(records)
        assert not conflicts[0].decided
        assert "no version is well enough supported" in conflicts[0].reason

    def test_duplicate_quotes_do_not_count_as_corroboration(self):
        """Copies of one document are not independent evidence."""
        same = "Ana is the Staff Engineer"
        records = [
            record("Ana", "has title", "Staff Engineer", f"d{i}", quote=same)
            for i in range(4)
        ] + [record("Ana", "has title", "Engineer", "d9", quote="different")]
        conflicts, _ = detect_conflicts(records, document_order=["d0", "d9"])
        winner = next(v for v in conflicts[0].versions
                      if v.display == "Staff Engineer")
        # Four copies of one sentence corroborate as little as one.
        assert winner.trust.corroboration <= 0.5

    def test_unmapped_predicates_are_reported_not_silently_dropped(self):
        records = [record("A", "frobnicates", "B", "d1"),
                   record("A", "frobnicates", "C", "d2")]
        conflicts, stats = detect_conflicts(records)
        assert conflicts == []
        assert stats["unmapped_predicates"] == 1

    def test_trust_breakdown_is_reported_per_component(self):
        """The interface has to explain a decision, not just assert it."""
        records = [
            record("Z", "has title", "A", "d1"),
            record("Z", "has title", "B", "d2"),
        ]
        conflicts, _ = detect_conflicts(records, document_order=["d1", "d2"])
        payload = conflicts[0].as_dict()
        trust = payload["versions"][0]["trust"]
        assert {"authority", "corroboration", "directness", "recency",
                "weights", "score"} <= set(trust)
