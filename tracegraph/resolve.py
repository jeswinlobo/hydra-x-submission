"""Entity resolution: deciding when two surfaces are one person.

The corpus makes this genuinely hard rather than incidentally hard. Slack is
55.8% of it and its speakers are bare first names, while Gmail supplies full
names and addresses. A single `sam:` line has at least ten plausible referents
in this corpus — Sam Carter, Sam Patel, Sam Wilson, Sam Wong, Samir Patel,
Samir Desai, Samantha Lee, Samuel Price, Samira Khan, Sam Irving. String
similarity cannot separate them, and picking the most frequent would be a
guess dressed up as an answer.

So resolution runs in tiers, and every decision records the method and the
evidence behind it:

1. **Strong key.** An email address identifies a person outright.
2. **Exact token bridge.** `alyssa.chen` and `Alyssa Chen` share the full token
   set `{alyssa, chen}`. Unambiguous when exactly one candidate matches.
3. **Graph evidence.** A partial match — a bare first name — is resolved only
   when the graph separates the candidates: shared channels, co-participation,
   a shared thread. This is the tier that needs HydraDB, and the tier the demo
   shows.
4. **Unresolved.** When evidence does not separate the candidates, the mention
   stays unresolved with its candidates recorded. PLAN.md is explicit that
   preserving an unresolved state beats forcing a weak match, and an honest
   "cannot tell" is a feature of a truth debugger rather than a gap in it.

Nothing here consults `eval-oracle/employee_directory.yaml`. That file maps
every person to their email and manager, which is precisely the answer this
module has to derive; it is scoring material only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .parsers.base import (
    BOT,
    PERSON,
    Mention,
    email_domain,
    email_local_part,
    name_tokens,
    normalise_name,
    organisation_root,
)

# Resolution methods, recorded on every RESOLVES_TO edge so a decision can be
# audited and so the UI can explain why one candidate beat another.
METHOD_STRONG_KEY = "strong_key_email"
METHOD_TOKEN_EXACT = "token_set_exact"
METHOD_TOKEN_UNIQUE = "token_subset_unique"
METHOD_GRAPH_EVIDENCE = "graph_evidence"
METHOD_UNRESOLVED = "unresolved"

# Confidence is reported, not invented: these are the ceilings each method can
# claim, and graph evidence is scored within its band by how decisively the
# neighbourhood separates the candidates.
CONFIDENCE = {
    METHOD_STRONG_KEY: 1.0,
    METHOD_TOKEN_EXACT: 0.95,
    METHOD_TOKEN_UNIQUE: 0.85,
    METHOD_GRAPH_EVIDENCE: 0.80,
    METHOD_UNRESOLVED: 0.0,
}


# Two organisation roots that differ only by a trailing word are one company:
# `redwood.ai` and `redwoodinference.com` both belong to Redwood Inference, and
# the hyphenated `redwood-inference.com` already reduces to `redwood`. Splitting
# those apart would undo the merge this module exists to perform — it costs 38
# genuine pairs on this corpus. The prefix must be long enough to mean
# something, so a four-letter company name cannot swallow a longer unrelated one.
_ROOT_PREFIX_MIN = 5


def _same_organisation(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _ROOT_PREFIX_MIN and longer.startswith(shorter)


def pack(values: Iterable[str], cap: int) -> str:
    """Join values with `;`, dropping whole values rather than cutting one.

    A plain `";".join(...)[:cap]` severs the last address mid-string, and
    `grace_oconnor@redwood.ai` became the entry `grace_oco` in the graph. That
    only mattered once entities were read back — a fragment then looks like a
    real address, mints a junk name token, and widens every candidate match for
    that person. Truncating at a separator keeps every surviving entry true.
    """
    packed: list[str] = []
    length = 0
    for value in values:
        extra = len(value) + (1 if packed else 0)
        if length + extra > cap:
            break
        packed.append(value)
        length += extra
    return ";".join(packed)


@dataclass
class Person:
    """A canonical person assembled from the documents alone."""

    key: str                      # canonical natural key, the id is derived from it
    display_name: str
    emails: set[str] = field(default_factory=set)
    handles: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    channels: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)

    @property
    def tokens(self) -> set[str]:
        """Tokens that may identify this person.

        When the display name is itself a bare address, only its local part
        counts: tokenising the whole thing folds the domain in, so every
        colleague at `redwood.ai` acquires the token `redwood` and a message
        mentioning the company resolves to a person.
        """
        name = self.display_name
        toks = name_tokens(email_local_part(name) if "@" in name else name)
        for email in self.emails:
            toks |= name_tokens(email_local_part(email))
        return toks


@dataclass
class Resolution:
    """One mention's resolution, with the evidence that produced it."""

    surface: str
    doc_id: str
    method: str
    confidence: float
    person_key: str | None
    candidates: list[str] = field(default_factory=list)
    evidence: str = ""

    @property
    def resolved(self) -> bool:
        return self.person_key is not None


class Resolver:
    """Builds canonical people from mentions, then resolves ambiguous surfaces.

    Two passes by necessity: identities have to exist before a bare handle can
    be matched against them, and the graph evidence that separates candidates
    only exists once participation has been recorded.
    """

    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self._by_email: dict[str, str] = {}
        self._by_token_set: dict[frozenset[str], set[str]] = defaultdict(set)
        # handle -> channels it spoke in, and the documents it appeared in.
        self._handle_channels: dict[str, set[str]] = defaultdict(set)
        self._handle_docs: dict[str, set[str]] = defaultdict(set)

    # --- pass one: build identities from strong evidence ---------------------

    def observe(self, doc_id: str, source_type: str, mentions: Iterable[Mention],
                channel: str | None = None) -> None:
        """Record one document's mentions.

        Only mentions carrying an email address create a person. A bare handle
        never does: it would create one identity per spelling and guarantee that
        every later match is against noise.
        """
        for mention in mentions:
            if mention.kind == BOT:
                continue
            email = mention.attributes.get("email")
            if email:
                self._observe_person(doc_id, mention, email, channel)
            elif mention.attributes.get("handle"):
                handle = mention.attributes["handle"]
                self._handle_docs[handle].add(doc_id)
                if channel:
                    self._handle_channels[handle].add(channel)

    def _observe_person(self, doc_id: str, mention: Mention, email: str,
                        channel: str | None) -> None:
        email = email.casefold()
        key = self._by_email.get(email) or f"email:{email}"
        person = self.people.get(key)
        if person is None:
            person = Person(key=key, display_name=mention.surface.strip())
            self.people[key] = person
        # Prefer a display name over a bare address as the label.
        if "@" in person.display_name and "@" not in mention.surface:
            person.display_name = mention.surface.strip()

        person.emails.add(email)
        person.domains.add(email_domain(email))
        person.documents.add(doc_id)
        if channel:
            person.channels.add(channel)
        self._by_email[email] = key
        self._by_token_set[frozenset(person.tokens)].add(key)

    # --- pass two: resolve surfaces -----------------------------------------

    def candidates_for(self, surface: str) -> list[str]:
        """People whose token set contains every token of this surface.

        A bare first name is a subset of many people; a full name usually of
        one. Subset rather than equality, because the surface is the shorter
        side.
        """
        tokens = name_tokens(surface)
        if not tokens:
            return []
        return sorted(
            key for key, person in self.people.items() if tokens <= person.tokens
        )

    def adopt(self, key: str, display_name: str, emails: Iterable[str],
              domains: Iterable[str] = (), channels: Iterable[str] = ()) -> None:
        """Take on a person the graph already holds.

        Bulk loading observes every document at once, so by the time a surface
        is resolved every identity in the batch exists. On-demand ingestion sees
        one document, and a resolver built from that document alone knows only
        the people it names — so `sam` would find no candidate and go
        unresolved, even though the graph has known Sam Okafor since the last
        question.

        Adopting the graph's entities gives the single-document resolver the
        same candidate pool the bulk pass had. The key is the entity's natural
        key, so the id derived from it is the id already in the graph and an
        adopted person resolves to the vertex that exists rather than a
        duplicate beside it.
        """
        person = self.people.get(key)
        if person is None:
            person = Person(key=key, display_name=display_name.strip() or key)
            self.people[key] = person
        elif "@" in person.display_name and "@" not in display_name:
            person.display_name = display_name.strip()

        for email in emails:
            email = email.casefold().strip()
            if email:
                person.emails.add(email)
                self._by_email[email] = key
        person.domains.update(d for d in domains if d)
        person.channels.update(c for c in channels if c)
        self._by_token_set[frozenset(person.tokens)].add(key)

    def merge_same_person(self, protected: Iterable[str] = ()) -> int:
        """Fold alternate addresses for one person into a single identity.

        The corpus gives the same person several addresses — Grace O'Connor
        appears at redwood.com, redwood.ai, redwood-inference.com and more —
        and one identity per address produced nineteen Grace O'Connors, each
        with a fragment of her evidence.

        Merging is restricted to full names, two tokens or more. A single-token
        display name is exactly the ambiguous case this module exists to be
        careful about: merging every `sam` would be the false merge that
        entity resolution is judged on.

        A shared full name is not on its own enough, because two people can have
        one. What made Grace's addresses hers was that they are all the same
        organisation spelled differently, so the merge also requires an
        organisational root in common — `redwood` across redwood.com,
        redwood.ai and redwood-inference.com. Priya Sharma at mediloop.com and
        Priya Sharma at procureco.com share a name and nothing else, and folding
        them together is the same false merge as folding every `sam`, only
        harder to notice.

        `protected` names identities that already exist as vertices in the
        graph. Those are never popped: a caller resolving one document at a time
        holds people the graph persisted long ago, and folding one into another
        here would leave its vertex and every edge pointing at it stranded,
        while new mentions of its address resolved to somebody else entirely.
        New people fold into a protected identity, never the reverse, and two
        protected identities are left alone.
        """
        protected = set(protected)
        by_name: dict[str, list[str]] = defaultdict(list)
        for key, person in self.people.items():
            tokens = name_tokens(person.display_name)
            if "@" in person.display_name or len(tokens) < 2:
                continue
            by_name[" ".join(sorted(tokens))].append(key)

        merged = 0
        for keys in by_name.values():
            if len(keys) < 2:
                continue
            for group in self._by_organisation(keys):
                merged += self._merge_group(group, protected)

        self._by_token_set.clear()
        for key, person in self.people.items():
            self._by_token_set[frozenset(person.tokens)].add(key)
        return merged

    def _by_organisation(self, keys: Sequence[str]) -> list[list[str]]:
        """Split same-named people into groups that share an organisation.

        Grouping is by the union of organisational roots, so a person known at
        both redwood.com and redwood.ai joins the same group as one known only
        at redwood-inference.com. Someone sharing no root with anybody stays in
        a group of one and is therefore never merged.
        """
        groups: list[tuple[set[str], list[str]]] = []
        for key in sorted(keys):
            roots = {organisation_root(d) for d in self.people[key].domains}
            roots.discard("")
            for existing_roots, members in groups:
                if any(_same_organisation(a, b)
                       for a in roots for b in existing_roots):
                    existing_roots |= roots
                    members.append(key)
                    break
            else:
                groups.append((roots, [key]))
        return [members for _, members in groups]

    def _merge_group(self, keys: Sequence[str], protected: set[str]) -> int:
        if len(keys) < 2:
            return 0
        anchors = [k for k in keys if k in protected]
        if len(anchors) > 1:
            # Both already exist in the graph. Merging them here would strand
            # one vertex; if they really are one person that is a repair for a
            # caller holding the whole picture, not for one document.
            return 0

        survivor = self.people[anchors[0] if anchors else keys[0]]
        merged = 0
        for key in keys:
            if key == survivor.key or key in protected:
                continue
            other = self.people.pop(key)
            survivor.emails |= other.emails
            survivor.domains |= other.domains
            survivor.channels |= other.channels
            survivor.documents |= other.documents
            for email in other.emails:
                self._by_email[email] = survivor.key
            merged += 1
        return merged

    def resolve_mention(
        self,
        mention: Mention,
        doc_id: str,
        channel: str | None = None,
        *,
        use_graph_tier: bool = True,
    ) -> Resolution:
        surface = mention.surface
        if mention.kind == BOT:
            return Resolution(surface, doc_id, METHOD_UNRESOLVED, 0.0, None,
                              evidence="automation, not a person")

        # Tier 1 — an address resolves outright.
        email = mention.attributes.get("email")
        if email and email.casefold() in self._by_email:
            key = self._by_email[email.casefold()]
            return Resolution(surface, doc_id, METHOD_STRONG_KEY,
                              CONFIDENCE[METHOD_STRONG_KEY], key,
                              evidence=f"email {email.casefold()}")

        candidates = self.candidates_for(surface)
        if not candidates:
            return Resolution(surface, doc_id, METHOD_UNRESOLVED, 0.0, None,
                              evidence="no candidate shares this surface's tokens")

        # Tier 2 — exactly one candidate.
        if len(candidates) == 1:
            key = candidates[0]
            if name_tokens(surface) == self.people[key].tokens:
                return Resolution(
                    surface, doc_id, METHOD_TOKEN_EXACT,
                    CONFIDENCE[METHOD_TOKEN_EXACT], key, candidates=candidates,
                    evidence=f"token set {sorted(name_tokens(surface))} matches "
                             "this person's exactly",
                )
            # A partial match against a single candidate is weaker than an exact
            # one and is labelled as such rather than borrowing the exact tier's
            # name: `chen` matching only Alex Chen is a unique subset, not an
            # identity, and calling it exact overstates it.
            return Resolution(
                surface, doc_id, METHOD_TOKEN_UNIQUE,
                CONFIDENCE[METHOD_TOKEN_UNIQUE], key, candidates=candidates,
                evidence=f"tokens {sorted(name_tokens(surface))} are a subset of "
                         "exactly one person's, with no competing candidate",
            )

        # Tier 3 — several candidates. Only the graph can separate them, and it
        # is queried by the caller that holds a client; this module reports the
        # candidate set rather than guessing from in-memory state.
        if not use_graph_tier:
            return Resolution(
                surface, doc_id, METHOD_UNRESOLVED, 0.0, None,
                candidates=list(candidates),
                evidence=f"{len(candidates)} candidates share the tokens "
                         f"{sorted(name_tokens(surface))}; needs graph evidence",
            )
        handle = mention.attributes.get("handle") or normalise_name(surface)
        return self._resolve_by_evidence(surface, doc_id, handle, candidates, channel)

    def _resolve_by_evidence(
        self, surface: str, doc_id: str, handle: str,
        candidates: Sequence[str], channel: str | None,
    ) -> Resolution:
        """Separate candidates by where they appear, not by how they are spelled.

        The signal is shared context: a handle that speaks in a channel is more
        likely to be the person who also participates there. A tie is left
        unresolved rather than broken arbitrarily.
        """
        context = set(self._handle_channels.get(handle, set()))
        if channel:
            context.add(channel)

        scored: list[tuple[int, str]] = []
        for key in candidates:
            overlap = len(context & self.people[key].channels)
            scored.append((overlap, key))
        scored.sort(reverse=True)

        best, runner_up = scored[0], (scored[1] if len(scored) > 1 else (0, ""))
        if best[0] == 0 or best[0] == runner_up[0]:
            return Resolution(
                surface, doc_id, METHOD_UNRESOLVED, 0.0, None,
                candidates=list(candidates),
                evidence=(
                    f"{len(candidates)} candidates share the tokens "
                    f"{sorted(name_tokens(surface))} and the graph does not "
                    "separate them"
                ),
            )

        # Confidence scales with how decisively the winner leads.
        #
        # Known weakness, measured on a single-channel slice: with one channel
        # in play the margin is always 1 - 0, so every evidence-tier resolution
        # reports the same confidence regardless of how thin the evidence is,
        # and an external-domain candidate can win an internal channel on one
        # incidental overlap. The score is only meaningful once a slice spans
        # several channels. Until it does, treat the tier as a candidate
        # ranking rather than a decision, and prefer widening the slice over
        # tuning this arithmetic.
        margin = (best[0] - runner_up[0]) / best[0]
        confidence = CONFIDENCE[METHOD_GRAPH_EVIDENCE] * (0.6 + 0.4 * margin)
        shared = sorted(context & self.people[best[1]].channels)[:3]
        return Resolution(
            surface, doc_id, METHOD_GRAPH_EVIDENCE, round(confidence, 3), best[1],
            candidates=list(candidates),
            evidence=(
                f"shares {best[0]} channel(s) {shared} with this handle, against "
                f"{runner_up[0]} for the next candidate"
            ),
        )

    def learn_participation(self, resolutions: Iterable["Resolution"],
                            channel_by_doc: dict[str, str]) -> int:
        """Feed confident resolutions back as participation evidence.

        Identities are built from email, which carries no channel, so on the
        first pass a person has no participation and the graph-evidence tier has
        nothing to compare against. Once a mention has been resolved by strong
        key inside a Slack document, that person demonstrably participates in
        that channel, and a second pass can use it to separate candidates that
        the first pass had to leave ambiguous.

        Only high-confidence resolutions feed back. Learning from a guess and
        then resolving further guesses against it is how a resolver talks itself
        into a false merge.
        """
        learned = 0
        for resolution in resolutions:
            if not resolution.resolved or resolution.method == METHOD_GRAPH_EVIDENCE:
                continue
            channel = channel_by_doc.get(resolution.doc_id)
            if not channel:
                continue
            person = self.people.get(resolution.person_key or "")
            if person is not None and channel not in person.channels:
                person.channels.add(channel)
                learned += 1
        return learned

    # --- reporting ----------------------------------------------------------

    def ambiguous_surfaces(self, minimum: int = 2) -> dict[str, list[str]]:
        """Surfaces with several candidates — the cases worth demonstrating."""
        out: dict[str, list[str]] = {}
        for handle in self._handle_channels:
            candidates = self.candidates_for(handle)
            if len(candidates) >= minimum:
                out[handle] = candidates
        return out
