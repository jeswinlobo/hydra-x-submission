"""Near-duplicate detection, tested against what it is for.

Corroboration counts distinct supporting documents, and a near-duplicate counted
twice inflates whichever version happened to be copied. So the property that
matters is not "finds similar documents" but "does not call two *different*
documents the same" — a false positive suppresses genuine corroboration and
makes the system trust the less-supported version. Most of these tests push on
the negative side for that reason, and the threshold itself was derived from the
separation these cases show rather than chosen in advance.
"""

from __future__ import annotations

import pytest

from tracegraph.neardup import (
    DEFAULT_THRESHOLD,
    exact_jaccard,
    find_near_duplicates,
    shingles,
    signature,
    similarity,
)

RUNBOOK = (
    "This runbook describes the operational procedures to deploy, upgrade, "
    "roll back, and safely disable the perf-canary service across regions. "
    "perf-canary is a lightweight always-on synthetic workload that calls "
    "internal inference endpoints for a curated model set and emits "
    "performance metrics. The service must stay under the defined overhead "
    "budget and must not capture or emit customer data."
)

# The same runbook pasted into an onboarding kit with a few words changed.
RUNBOOK_COPY = RUNBOOK.replace("safely disable", "cleanly disable").replace(
    "across regions", "across all regions")

# Same domain, same vocabulary, entirely different content. This is the case
# a token-overlap measure gets wrong and shingling gets right.
OTHER_INFRA = (
    "This postmortem covers the regional failover that began when the "
    "inference endpoints in eu-central started returning elevated latency. "
    "The on-call engineer rolled traffic to a secondary region and the "
    "service recovered within the defined budget. Customer data was not "
    "affected and no metrics were lost during the window."
)


class TestItFindsRealCopies:
    def test_a_document_is_identical_to_itself(self):
        sig = signature(RUNBOOK)
        assert similarity(sig, sig) == 1.0

    def test_a_lightly_edited_copy_is_a_near_duplicate(self):
        score = similarity(signature(RUNBOOK), signature(RUNBOOK_COPY))
        assert score >= DEFAULT_THRESHOLD

    def test_case_and_punctuation_do_not_matter(self):
        loud = RUNBOOK.upper() + "!!!"
        assert similarity(signature(RUNBOOK), signature(loud)) == 1.0

    def test_the_estimate_tracks_the_true_jaccard(self):
        """MinHash is an estimator; it has to actually estimate the thing."""
        est = similarity(signature(RUNBOOK), signature(RUNBOOK_COPY))
        true = exact_jaccard(RUNBOOK, RUNBOOK_COPY)
        assert abs(est - true) < 0.15


class TestItRefusesFalsePositives:
    def test_same_topic_different_content_is_not_a_duplicate(self):
        """The expensive mistake: shared vocabulary is not shared meaning."""
        score = similarity(signature(RUNBOOK), signature(OTHER_INFRA))
        assert score < DEFAULT_THRESHOLD

    def test_unrelated_text_scores_near_zero(self):
        score = similarity(signature(RUNBOOK), signature(
            "The quarterly revenue target for the retail division was revised "
            "upward after the marketplace partnership closed."))
        assert score < 0.2

    def test_an_empty_document_resembles_nothing(self):
        """Two empty bodies must not collapse into one duplicate class."""
        assert signature("") == ()
        assert similarity(signature(""), signature("")) == 0.0
        assert similarity(signature(""), signature(RUNBOOK)) == 0.0

    def test_the_threshold_sits_in_the_empty_band(self):
        """The measured separation the threshold was chosen from.

        If either bound moves, the constant needs re-deriving rather than
        nudging — this test is what makes that visible.
        """
        true_positive = similarity(signature(RUNBOOK), signature(RUNBOOK_COPY))
        false_positive = similarity(signature(RUNBOOK), signature(OTHER_INFRA))
        assert false_positive < DEFAULT_THRESHOLD < true_positive

    def test_a_shared_boilerplate_header_is_not_enough(self):
        header = "Confidential. Internal use only. Do not distribute. "
        a = header + "The rollout was paused after latency regressed."
        b = header + "The quarterly forecast was revised after the deal closed."
        assert similarity(signature(a), signature(b)) < DEFAULT_THRESHOLD


class TestShingles:
    def test_short_text_still_produces_one_shingle(self):
        assert len(shingles("only three words")) == 1

    def test_empty_text_produces_none(self):
        assert shingles("") == set()

    def test_word_order_matters(self):
        """This is the whole reason for shingling rather than a token set."""
        a = "the service must not emit customer data at any time"
        b = "customer data must not emit the service at any time"
        assert shingles(a) != shingles(b)

    def test_signatures_are_stable_across_calls(self):
        """Permutations are seeded, so a stored signature stays comparable."""
        assert signature(RUNBOOK) == signature(RUNBOOK)


class TestFindNearDuplicates:
    def test_it_pairs_the_copy_and_leaves_the_others(self):
        found = find_near_duplicates({
            "a": RUNBOOK, "b": RUNBOOK_COPY, "c": OTHER_INFRA,
        })
        assert len(found) == 1
        assert {found[0].left, found[0].right} == {"a", "b"}
        assert found[0].similarity >= DEFAULT_THRESHOLD

    def test_each_pair_is_reported_once(self):
        found = find_near_duplicates({"a": RUNBOOK, "b": RUNBOOK, "c": RUNBOOK})
        assert len(found) == 3  # ab, ac, bc — not six

    def test_results_are_ordered_by_similarity(self):
        found = find_near_duplicates({
            "a": RUNBOOK, "b": RUNBOOK, "c": RUNBOOK_COPY,
        })
        assert found == sorted(found, key=lambda d: -d.similarity)

    def test_no_pairs_below_threshold(self):
        assert find_near_duplicates({"a": RUNBOOK, "c": OTHER_INFRA}) == []

    def test_an_empty_corpus_is_not_an_error(self):
        assert find_near_duplicates({}) == []

    @pytest.mark.parametrize("threshold", [0.4, 0.9, 1.0])
    def test_the_threshold_is_respected(self, threshold):
        found = find_near_duplicates(
            {"a": RUNBOOK, "b": RUNBOOK_COPY, "c": OTHER_INFRA},
            threshold=threshold)
        assert all(d.similarity >= threshold for d in found)


class TestCorroborationDiscount:
    """The reason this module exists: near-duplicates must change a verdict.

    Detecting duplicates and then not using them would be decoration. These
    tests pin the one place the result matters — corroboration counts
    independent documents, and an edited copy is not one.
    """

    @staticmethod
    def _version(dsids):
        from tracegraph.conflicts import ClaimRecord, Version
        v = Version(value="vp customer success", display="VP, Customer Success")
        # Distinct quotes, so the *existing* identical-quote discount cannot
        # fire and only the near-duplicate map can change the count.
        for i, d in enumerate(dsids):
            v.claims.append(ClaimRecord(
                claim_id=i, dsid=d, source_type="gmail",
                subject="Marissa Cole", predicate="holds_title",
                object="VP, Customer Success", confidence=0.9,
                quote=f"distinct phrasing number {i}"))
        return v

    def test_two_copies_count_as_one_source(self):
        from tracegraph.conflicts import _corroboration
        version = self._version(["a", "b"])
        without = _corroboration(version, 4)
        with_dupes = _corroboration(version, 4, {"b": "a"})
        assert with_dupes < without

    def test_unrelated_documents_are_unaffected(self):
        from tracegraph.conflicts import _corroboration
        version = self._version(["a", "b"])
        assert _corroboration(version, 4) == _corroboration(version, 4, {"z": "y"})

    def test_a_cluster_of_three_collapses_to_one(self):
        from tracegraph.conflicts import _corroboration
        version = self._version(["a", "b", "c"])
        collapsed = _corroboration(version, 6, {"b": "a", "c": "a"})
        assert collapsed == _corroboration(self._version(["a"]), 6)

    def test_an_empty_map_changes_nothing(self):
        from tracegraph.conflicts import _corroboration
        version = self._version(["a", "b"])
        assert _corroboration(version, 4, {}) == _corroboration(version, 4)


class TestCanonicalMap:
    """Union-find, because similarity is not transitive."""

    @staticmethod
    def _dups(pairs):
        from tracegraph.neardup import NearDuplicate
        return [NearDuplicate(a, b, 0.9) for a, b in pairs]

    def test_a_chain_collapses_to_one_representative(self):
        """a~b and b~c must put a, b and c in ONE cluster, not two.

        This is the case a naive {right: left} dict gets wrong, and getting it
        wrong double-counts the cluster — the exact error this exists to avoid.
        """
        from tracegraph.neardup import canonical_map
        m = canonical_map(self._dups([("b", "c"), ("a", "b")]))
        roots = {m.get(x, x) for x in ("a", "b", "c")}
        assert len(roots) == 1

    def test_the_representative_is_stable_regardless_of_pair_order(self):
        from tracegraph.neardup import canonical_map
        a = canonical_map(self._dups([("a", "b"), ("b", "c")]))
        b = canonical_map(self._dups([("c", "b"), ("b", "a")]))
        assert {x: a.get(x, x) for x in "abc"} == {x: b.get(x, x) for x in "abc"}

    def test_separate_clusters_stay_separate(self):
        from tracegraph.neardup import canonical_map
        m = canonical_map(self._dups([("a", "b"), ("y", "z")]))
        assert m.get("b", "b") != m.get("z", "z")

    def test_documents_with_no_duplicate_are_absent(self):
        from tracegraph.neardup import canonical_map
        assert canonical_map(self._dups([("a", "b")])).keys() == {"b"}

    def test_no_duplicates_is_an_empty_map(self):
        from tracegraph.neardup import canonical_map
        assert canonical_map([]) == {}
