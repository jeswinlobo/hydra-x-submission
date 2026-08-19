"""The tier that answers the brief's own example.

Track 01 opens on *"deciding that 'Sam', '@soham' and 'S. Ratnaparkhi' are one
person"*. Two of those three resolve by token overlap. `Sam` does not and
cannot: `{sam}` is not a subset of `{soham, ratnaparkhi}`, so there is no
shared token, no small edit distance, and nothing for an embedding of two short
strings to recover. Before this tier the resolver returned UNRESOLVED with
"no candidate shares this surface's tokens" and stopped.

The graph can still reach it, because the relationship is structural rather than
lexical — Soham participates in the channel, Soham is already resolved elsewhere
in the document. So the candidate set comes from HydraDB and the existing
co-occurrence and participation traversals decide.

That makes it the weakest positive claim the resolver makes, and a wrong answer
is a false merge — the failure this whole module exists to refuse. So almost
every test here is about a guard refusing, not about the tier firing.
"""

from __future__ import annotations

import pytest

from tracegraph.ingest import ENTITY, OnDemandIngestor
from tracegraph.ids import node_identity
from tracegraph.parsers.base import PERSON, BOT, Mention
from tracegraph.resolve import CONFIDENCE, METHOD_GRAPH_PROPOSED


class FakeEvidence:
    """Stands in for HydraDB, returning whatever the graph is said to hold."""

    def __init__(self, proposed):
        self._proposed = proposed
        self.asked_with = []

    def propose_from_structure(self, initial, document_id, channel_id):
        self.asked_with.append((initial, document_id, channel_id))
        return self._proposed


class FakePerson:
    def __init__(self, display_name):
        self.display_name = display_name


class FakeResolver:
    def __init__(self, people=None):
        self.people = people or {}


def mention(surface, kind=PERSON):
    return Mention(surface=surface, kind=kind, role="speaker", start=0,
                   end=len(surface))


def ingestor():
    """An ingestor husk — this method touches no state but its constants."""
    return OnDemandIngestor.__new__(OnDemandIngestor)


SOHAM_KEY = "email:soham.ratnaparkhi@redwood.ai"
SOHAM_ID = node_identity(ENTITY, SOHAM_KEY).id


def call(surface, proposed, *, kind=PERSON, entity_ids=None, channel_id=7):
    ev = FakeEvidence(proposed)
    return ingestor()._propose_from_structure(
        ev, mention(surface, kind), 42, channel_id,
        entity_ids if entity_ids is not None else {SOHAM_KEY: SOHAM_ID},
        FakeResolver({SOHAM_KEY: FakePerson("Soham Ratnaparkhi")})), ev


class TestTheBriefsExample:
    def test_sam_resolves_to_soham_from_structure_alone(self):
        """The headline case. No token overlap; the graph decides."""
        result, _ = call("sam", [(SOHAM_KEY, "Soham Ratnaparkhi", 4, 1, SOHAM_ID)])
        assert result is not None
        entity_id, reason, confidence = result
        assert entity_id == SOHAM_ID
        assert confidence == CONFIDENCE[METHOD_GRAPH_PROPOSED]
        assert "Soham Ratnaparkhi" in reason
        assert "4 co-mention" in reason and "1 shared channel" in reason

    def test_it_asks_the_graph_for_the_right_initial(self):
        _, ev = call("sam", [(SOHAM_KEY, "Soham Ratnaparkhi", 4, 1, SOHAM_ID)])
        assert ev.asked_with == [("s", 42, 7)]

    def test_participation_alone_is_enough(self):
        """A shared channel with no co-mention still counts as evidence."""
        result, _ = call("sam", [(SOHAM_KEY, "Soham Ratnaparkhi", 0, 2, SOHAM_ID)])
        assert result is not None

    def test_co_occurrence_alone_is_enough(self):
        result, _ = call("sam", [(SOHAM_KEY, "Soham Ratnaparkhi", 3, 0, SOHAM_ID)])
        assert result is not None


class TestTheGuardsRefuse:
    def test_two_scoring_candidates_is_an_abstention(self):
        """Two people with evidence means the graph cannot separate them.

        This is the false-merge guard. Picking the higher score here would be a
        coin toss dressed as a decision.
        """
        result, _ = call("sam", [
            (SOHAM_KEY, "Soham Ratnaparkhi", 4, 1, SOHAM_ID),
            ("email:samantha@acme.com", "Samantha Lewis", 2, 1, 12345),
        ])
        assert result is None

    def test_an_implausible_rival_does_not_block(self):
        """`sam` is not a subsequence of `Priya Nair`, so it is not a rival."""
        result, _ = call("sam", [
            (SOHAM_KEY, "Soham Ratnaparkhi", 4, 1, SOHAM_ID),
            ("email:priya@acme.com", "Priya Nair", 3, 1, 12345),
        ])
        assert result is not None

    def test_a_name_the_token_cannot_shorten_is_refused(self):
        """The false merge the shared initial alone would allow: ben -> Barbara."""
        result, _ = call("ben", [("email:b@acme.com", "Barbara Liu", 9, 9, 999)])
        assert result is None

    def test_an_empty_graph_proposal_is_an_abstention(self):
        result, _ = call("sam", [])
        assert result is None

    @pytest.mark.parametrize("surface", ["hi", "ok", "a", ""])
    def test_a_surface_too_short_to_be_a_name_is_refused(self, surface):
        """Below three characters this is noise, not a shortened name."""
        result, _ = call(surface, [(SOHAM_KEY, "Soham Ratnaparkhi", 9, 9, SOHAM_ID)])
        assert result is None

    def test_a_multi_token_surface_is_refused(self):
        """Two tokens means the string tiers had something to work with."""
        result, _ = call("sam carter", [(SOHAM_KEY, "Soham Ratnaparkhi", 9, 9, SOHAM_ID)])
        assert result is None

    def test_a_bot_is_never_a_person(self):
        result, _ = call("deploybot", [(SOHAM_KEY, "Soham Ratnaparkhi", 9, 9, SOHAM_ID)],
                         kind=BOT)
        assert result is None

    def test_a_surface_matching_the_name_is_left_to_the_string_tiers(self):
        """`soham` already resolves by token subset; claiming it here would
        misreport which tier decided."""
        result, _ = call("soham", [(SOHAM_KEY, "Soham Ratnaparkhi", 4, 1, SOHAM_ID)])
        assert result is None

    def test_an_unknown_entity_key_is_refused(self):
        """A proposal the id map cannot place is dropped, not guessed at."""
        result, _ = call("sam", [("email:ghost@nowhere.com", "Sam Ghost", 4, 1, None)],
                         entity_ids={})
        assert result is None

    def test_no_channel_still_works_through_co_occurrence(self):
        result, _ = call("sam", [(SOHAM_KEY, "Soham Ratnaparkhi", 5, 0, SOHAM_ID)],
                         channel_id=None)
        assert result is not None


class TestTheDecisionIsRecorded:
    def test_the_reason_states_what_the_graph_saw(self):
        """A decision nobody can audit is not better than a guess."""
        result, _ = call("sam", [(SOHAM_KEY, "Soham Ratnaparkhi", 4, 1, SOHAM_ID)])
        _, reason, _ = result
        assert "no candidate shares this surface's tokens" in reason
        assert "the graph proposed" in reason
        assert "sole scoring candidate" in reason
        assert "initial 's'" in reason

    def test_confidence_is_below_the_graph_evidence_tier(self):
        """This tier has no lexical corroboration, and its ceiling says so."""
        from tracegraph.resolve import METHOD_GRAPH_EVIDENCE
        assert (CONFIDENCE[METHOD_GRAPH_PROPOSED]
                < CONFIDENCE[METHOD_GRAPH_EVIDENCE])
