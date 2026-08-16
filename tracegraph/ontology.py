"""Canonical predicates, and the alignment of raw ones onto them.

Extraction produces 732 distinct predicates over 1,336 claims — `has title`,
`job title`, `has job title`, `has role`, `role`, `works as` all say the same
thing. Conflict detection is impossible against that vocabulary: two claims can
only disagree if they are first agreed to be about the same relation.

Alignment matters for a second reason that is easy to miss. Most predicates are
**multi-valued**, and for those, differing objects are not a disagreement at
all. `rollback criteria includes` legitimately has three objects; `maya proposed
action` has several; a person legitimately has several email addresses. Treating
every difference as a conflict would bury the handful of real ones — a person
holding three different job titles — under dozens of false ones.

So each canonical predicate carries:

* **cardinality** — `single` means one true value at a time, and only these can
  conflict. `multi` means values accumulate.
* **mutability** — whether the value legitimately changes over time. Recency is
  only evidence for a mutable predicate; for an immutable one a later claim
  disagreeing with an earlier one is a contradiction, not an update.
* **authority** — which sources are systems of record for it, best first. A
  title from an email signature outranks one inferred from chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SINGLE = "single"
MULTI = "multi"


@dataclass(frozen=True)
class Predicate:
    name: str
    cardinality: str
    mutable: bool
    # Source types that are systems of record for this relation, best first.
    authority: tuple[str, ...]
    synonyms: tuple[str, ...]

    @property
    def can_conflict(self) -> bool:
        return self.cardinality == SINGLE


# Seeded small and deliberately, per PLAN.md: a catalogue that guesses is worse
# than one that declines. Anything unmatched stays a raw predicate and is
# reported for review rather than being forced into a category.
CATALOG: tuple[Predicate, ...] = (
    Predicate(
        "holds_title", SINGLE, True, ("gmail", "fireflies", "confluence"),
        ("has title", "job title", "has job title", "title", "has role", "role",
         "works as", "serves as", "is title", "holds title", "position"),
    ),
    Predicate(
        "employed_by", SINGLE, True, ("gmail", "fireflies", "hubspot"),
        ("works at", "works for", "employed by", "employed at", "is employed by",
         "member of", "belongs to organisation"),
    ),
    Predicate(
        "owned_by", SINGLE, True, ("jira", "linear", "github", "confluence"),
        ("owner", "owns", "assigned to", "is assigned to", "has owner",
         "responsible for", "is owned by", "led by", "leads"),
    ),
    Predicate(
        "has_status", SINGLE, True, ("jira", "linear", "github"),
        ("has status", "status", "is status", "state", "has state",
         "current status"),
    ),
    Predicate(
        "due_on", SINGLE, True, ("jira", "linear", "confluence"),
        ("due", "due date", "due on", "eta", "action due date", "target date",
         "scheduled for", "deadline", "target signature", "expected by"),
    ),
    Predicate(
        "has_email", MULTI, False, ("gmail",),
        ("has email", "email", "email address", "contactable at"),
    ),
    Predicate(
        "includes", MULTI, False, (),
        ("includes", "contains", "comprises", "consists of", "has component"),
    ),
    Predicate(
        "requires", MULTI, False, (),
        ("requires", "depends on", "needs", "blocked on", "is blocked on",
         "requires engineer-days"),
    ),
    Predicate(
        "proposed_action", MULTI, False, (),
        ("proposed action", "proposes", "recommends", "suggested",
         "action item", "next step"),
    ),
    Predicate(
        "operates_in", MULTI, False, ("hubspot",),
        ("operates in", "located in", "based in", "present in"),
    ),
    Predicate(
        "references", MULTI, False, ("github", "linear", "jira"),
        ("references", "relates to", "linked to", "related work", "mentions"),
    ),
)

_BY_SYNONYM: dict[str, Predicate] = {}
for _p in CATALOG:
    _BY_SYNONYM[_p.name.replace("_", " ")] = _p
    for _s in _p.synonyms:
        _BY_SYNONYM[_s] = _p

_STOP = {"is", "are", "was", "were", "the", "a", "an", "of", "to", "has", "have"}


def normalise_predicate(raw: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").casefold())
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Alignment:
    raw: str
    predicate: Predicate | None
    method: str
    confidence: float

    @property
    def aligned(self) -> bool:
        return self.predicate is not None


def align(raw: str) -> Alignment:
    """Map a raw predicate onto the catalogue, or decline.

    Exact synonym match first, then a token-overlap match that still requires
    the raw predicate's content words to be a subset of a synonym's. Anything
    weaker is left unaligned: an unmapped relation is a queue item, not a
    licence to invent a category, and a wrong alignment manufactures conflicts
    between claims that were never about the same thing.
    """
    text = normalise_predicate(raw)
    if not text:
        return Alignment(raw, None, "empty", 0.0)

    hit = _BY_SYNONYM.get(text)
    if hit is not None:
        return Alignment(raw, hit, "exact", 1.0)

    tokens = {t for t in text.split() if t not in _STOP}
    if not tokens:
        return Alignment(raw, None, "stopwords_only", 0.0)

    best: tuple[float, Predicate | None] = (0.0, None)
    for synonym, predicate in _BY_SYNONYM.items():
        candidate = {t for t in synonym.split() if t not in _STOP}
        if not candidate:
            continue
        if tokens <= candidate or candidate <= tokens:
            score = len(tokens & candidate) / max(len(tokens | candidate), 1)
            if score > best[0]:
                best = (score, predicate)

    if best[1] is not None and best[0] >= 0.5:
        return Alignment(raw, best[1], "token_subset", round(0.6 + 0.3 * best[0], 3))
    return Alignment(raw, None, "unmapped", 0.0)


def normalise_object(raw: str) -> str:
    """Canonical form of a claim object, for comparing two claims.

    Deliberately shallow. Case and punctuation differences are noise; anything
    beyond that is a judgement about meaning, and making it here would hide
    disagreements the conflict panel exists to show.
    """
    text = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    # A common, safe equivalence: an organisation named with or without its
    # suffix is the same organisation.
    for suffix in (" inc", " ltd", " llc", " corp", " inference", " systems"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def source_authority(predicate: Predicate, source_type: str) -> float:
    """How authoritative this source is for this relation, in [0, 1].

    A predicate with no declared system of record scores neutrally rather than
    zero, so an absent authority profile does not silently outrank evidence.
    """
    if not predicate.authority:
        return 0.5
    if source_type in predicate.authority:
        rank = predicate.authority.index(source_type)
        return round(1.0 - 0.15 * rank, 3)
    return 0.25
