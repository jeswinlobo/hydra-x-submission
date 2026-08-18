"""Folding two identities into one, and refusing to when it would be wrong.

`merge_same_person` exists because the corpus gives one person several
addresses — Grace O'Connor appears under fourteen spellings of the same
employer, and one identity per address produced nineteen Grace O'Connors.

It is also the most dangerous function in the module, because a merge is
irreversible from the graph's point of view: the loser's vertex keeps its edges
while every later mention of its address resolves to the survivor. These tests
pin the three ways that goes wrong.
"""

from __future__ import annotations

import pytest

from tracegraph.parsers.base import PERSON, Mention, organisation_root
from tracegraph.resolve import Resolver, normalise_address, pack


def mention(surface: str, email: str, start: int = 0) -> Mention:
    return Mention(surface=surface, kind=PERSON, role="author", start=start,
                   end=start + len(surface), attributes={"email": email})


def resolver_with(*people: tuple[str, str]) -> Resolver:
    r = Resolver()
    for i, (name, email) in enumerate(people):
        r.observe(f"doc_{i}", "gmail", [mention(name, email, start=i * 100)])
    return r


# --- what the function is for -----------------------------------------------

def test_one_person_under_many_spellings_of_one_employer_merges():
    """The Grace case: same name, same company, several domains."""
    r = resolver_with(
        ("Grace O'Connor", "grace@redwood.com"),
        ("Grace O'Connor", "grace.oconnor@redwood.ai"),
        ("Grace O'Connor", "grace@redwood-inference.com"),
    )
    assert len(r.people) == 3
    assert r.merge_same_person() == 2
    assert len(r.people) == 1
    survivor = next(iter(r.people.values()))
    assert len(survivor.emails) == 3


def test_a_company_spelled_with_and_without_a_separator_is_one_company():
    """`redwood.ai` and `redwoodinference.com` are the same employer.

    Reducing a domain to its first label alone would split these, undoing the
    merge on 38 genuine pairs in this corpus.
    """
    assert organisation_root("redwood-inference.com") == "redwood"
    assert organisation_root("redwoodinference.com") == "redwoodinference"
    r = resolver_with(
        ("Aditya Rao", "aditya.rao@redwood.ai"),
        ("Aditya Rao", "aditya_rao@redwoodinference.com"),
    )
    assert r.merge_same_person() == 1, "one company, split by spelling"


# --- what it must refuse ----------------------------------------------------

def test_same_name_at_different_companies_does_not_merge():
    """Two people can share a name. That is not evidence they are one person."""
    r = resolver_with(
        ("Priya Sharma", "priya@mediloop.com"),
        ("Priya Sharma", "priya.sharma@procureco.com"),
    )
    assert r.merge_same_person() == 0
    assert len(r.people) == 2, "a shared name merged two companies' people"


def test_a_short_root_cannot_swallow_a_longer_unrelated_one():
    """Prefix matching is bounded, or `med` would absorb `mediloop`."""
    r = resolver_with(
        ("Dana Reid", "dana@arc.com"),
        ("Dana Reid", "dana@arcadia-health.com"),
    )
    assert r.merge_same_person() == 0, "'arc' is too short to claim 'arcadia'"


def test_a_persisted_identity_is_never_merged_away():
    """The regression this test exists for.

    On-demand ingestion adopts identities the graph already holds, then merges.
    Without protection the merge popped a persisted person, leaving its vertex
    and every edge on it stranded while new mentions of its address resolved to
    somebody else — at `strong_key_email`, confidence 1.0.
    """
    r = Resolver()
    r.adopt("email:priya@mediloop.com", "Priya Sharma",
            ["priya@mediloop.com"], ["mediloop.com"])
    r.adopt("email:priya.sharma@procureco.com", "Priya Sharma",
            # As the damaged graph held it: procureco having absorbed mediloop,
            # which is what made the two look like one organisation.
            ["priya.sharma@procureco.com", "priya@mediloop.com"],
            ["procureco.com", "mediloop.com"])
    persisted = set(r.people)

    assert r.merge_same_person(protected=persisted) == 0
    assert set(r.people) == persisted, "a vertex that exists was merged away"


def test_a_new_person_folds_into_the_persisted_one_not_the_reverse():
    """Direction matters: the survivor must be the identity with the vertex."""
    r = Resolver()
    r.adopt("email:grace@redwood.com", "Grace O'Connor",
            ["grace@redwood.com"], ["redwood.com"])
    r.observe("doc_new", "gmail",
              [mention("Grace O'Connor", "grace.oconnor@redwood.ai")])

    assert r.merge_same_person(protected={"email:grace@redwood.com"}) == 1
    assert set(r.people) == {"email:grace@redwood.com"}
    assert r._by_email["grace.oconnor@redwood.ai"] == "email:grace@redwood.com"


# --- the truncation that fed the merge --------------------------------------

@pytest.mark.parametrize("cap,expected", [
    (400, "a@x.com;b@y.com"),
    (15, "a@x.com;b@y.com"),   # exactly fits: 7 + separator + 7
    (14, "a@x.com"),           # one short, so the second is dropped whole
    (7, "a@x.com"),
    (3, ""),
])
def test_pack_drops_whole_values_rather_than_severing_one(cap, expected):
    """`";".join(...)[:cap]` produced the graph entry `grace_oco`.

    A severed address read back as real mints a permanent fake address and a
    junk name token, which widens every candidate match for that person.
    """
    assert pack(["a@x.com", "b@y.com"], cap) == expected


def test_packed_addresses_are_all_still_addresses():
    emails = [f"person{i}@somewhat-long-domain-name.example.com" for i in range(40)]
    packed = pack(sorted(emails), 400)
    assert packed, "the cap should still admit several addresses"
    assert all("@" in part for part in packed.split(";"))
    assert len(packed) <= 400


# --- canonicalisation has to outlive the process ----------------------------


def test_a_folded_identity_is_recorded_not_just_forgotten():
    """A merge that lives only in a resolver lasts until the process exits.

    The loser's vertex stays in the graph — deleting it would strand whatever
    references it — so the next resolver adopts it again as its own protected
    identity, and two protected identities are never merged. `Camila Reyes`
    came back to six candidates on every restart while mention-level splits
    read as fixed. The decision has to be written down.
    """
    r = Resolver()
    for local in ("camila.reyes", "camila_reyes"):
        r.observe(f"doc_{local}", "gmail",
                  [mention("Camila Reyes", f"{local}@redwood.ai")])
    assert len(r.people) == 2
    assert r.merge_same_person() == 1

    survivor = next(iter(r.people))
    folded = "email:camila_reyes@redwood.ai"
    assert folded not in r.people, "the loser is still a live identity"
    # The survivor must be reachable from the folded address, which is what a
    # persisted MERGED_INTO edge encodes.
    assert r._by_email["camila_reyes@redwood.ai"] == survivor


def test_re_adopting_a_folded_identity_undoes_the_merge():
    """The exact failure, reproduced: this is why adoption must skip them.

    Both vertices exist in the graph after a merge. Adopting both makes them
    protected, `merge_same_person` refuses to fold two protected identities,
    and the surface resolves to several candidates again.
    """
    naive = Resolver()
    naive.adopt("email:camila.reyes@redwood.ai", "Camila Reyes",
                ["camila.reyes@redwood.ai"], ["redwood.ai"])
    naive.adopt("email:camila_reyes@redwood.ai", "Camila Reyes",
                ["camila_reyes@redwood.ai"], ["redwood.ai"])
    naive.merge_same_person(protected=set(naive.people))
    assert len(naive.candidates_for("Camila Reyes")) == 2, (
        "adopting both vertices should reproduce the split")

    # Skipping the folded one — what `OnDemandIngestor._adopt_known` now does
    # using the graph's MERGED_INTO edges — restores a single candidate.
    canonical = Resolver()
    canonical.adopt("email:camila.reyes@redwood.ai", "Camila Reyes",
                    ["camila.reyes@redwood.ai", "camila_reyes@redwood.ai"],
                    ["redwood.ai"])
    assert canonical.candidates_for("Camila Reyes") == [
        "email:camila.reyes@redwood.ai"]


def test_a_doubled_address_normalises_to_one():
    """`naomi.feldman@naomi.feldman@redwood.com` reached the graph as a key,
    which made that person unreachable under the address she actually uses."""
    assert (normalise_address("naomi.feldman@naomi.feldman@redwood.com")
            == "naomi.feldman@redwood.com")
    assert normalise_address("ordinary@redwood.com") == "ordinary@redwood.com"

    r = Resolver()
    r.observe("doc", "gmail", [mention(
        "Naomi Feldman", "naomi.feldman@naomi.feldman@redwood.com")])
    assert list(r.people) == ["email:naomi.feldman@redwood.com"]
