"""Conflict detection and trust scoring.

Two claims conflict when they are about the same subject and the same canonical
relation, that relation holds one value at a time, and their objects differ.
All three conditions matter: without the middle one, `rollback criteria
includes` looks like a three-way contradiction rather than a list.

Nothing here discards the losing claim. PLAN.md is explicit that a conflict
answer returns every materially supported version with its citations, and only
names a best-supported reading when the evidence justifies one. A truth debugger
that quietly picks a winner is just a search engine with extra steps.

Trust is decomposed rather than reduced to a number, so the interface can show
why one version leads:

* **authority** — is this source a system of record for this relation?
* **directness** — a first-party statement outranks a report of one.
* **corroboration** — how many independent documents assert the same value,
  after copies of one another are discounted.
* **recency** — only for a mutable relation. A newer title supersedes an older
  one; a newer contradiction about an immutable fact is just a contradiction.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .ontology import Predicate, align, normalise_object, source_authority

# Weights are stated here rather than buried in the arithmetic so a reader can
# disagree with them. Authority dominates deliberately: PLAN.md is explicit that
# graph centrality and popularity must never outrank a system of record.
#
# The weighting depends on whether the relation can legitimately change, and the
# difference is the whole point of separating the two. For an **immutable**
# relation, two different values are a contradiction and lateness proves
# nothing, so recency barely counts. For a **mutable** one — a job title, a
# ticket status, a due date — the later statement is normally not a rival
# version at all but the current one superseding an old one, so recency has to
# be able to decide. Giving both the same weights, as a first pass did, made
# every job-title disagreement undecidable: with all evidence from one source
# type the only varying component was recency, and at a weight of 0.10 it could
# never clear a 0.15 margin.
WEIGHTS_IMMUTABLE = {
    "authority": 0.45, "corroboration": 0.25, "directness": 0.20, "recency": 0.10,
}
# Authority stays the largest weight even here, and the margin is deliberate.
# The spread between a system of record and an incidental mention is about 0.75,
# and the spread between oldest and newest is 1.0, so authority only outranks
# recency while its weight exceeds recency's by more than a third. At 0.45
# against 0.30 a ticket's own tracker beats a later remark in chat (0.34 against
# 0.30), while two statements from the same kind of source are separated by
# recency alone (0.30, comfortably past the decisive margin). Set them any
# closer and a passing mention in Slack silently overrides Jira, which is the
# failure PLAN.md rules out when it says centrality and popularity must never
# dominate source-of-record evidence. A test pins both halves.
WEIGHTS_MUTABLE = {
    "authority": 0.45, "corroboration": 0.13, "directness": 0.12, "recency": 0.30,
}

# A winner has to lead by this much before it is called the best-supported
# reading. Below it the versions are presented without one.
DECISIVE_MARGIN = 0.15


@dataclass
class ClaimRecord:
    """One claim as stored, with what is needed to weigh it."""

    claim_id: int
    dsid: str
    source_type: str
    subject: str
    predicate: str
    object: str
    confidence: float
    quote: str = ""
    timestamp: int | None = None


@dataclass
class TrustBreakdown:
    authority: float
    corroboration: float
    directness: float
    recency: float
    mutable: bool = False

    @property
    def weights(self) -> dict[str, float]:
        return WEIGHTS_MUTABLE if self.mutable else WEIGHTS_IMMUTABLE

    @property
    def score(self) -> float:
        w = self.weights
        return round(
            self.authority * w["authority"]
            + self.corroboration * w["corroboration"]
            + self.directness * w["directness"]
            + self.recency * w["recency"],
            4,
        )

    def as_dict(self) -> dict:
        return {
            "authority": self.authority, "corroboration": self.corroboration,
            "directness": self.directness, "recency": self.recency,
            "weights": self.weights, "score": self.score,
        }


@dataclass
class Version:
    """One asserted value for a contested fact, with everything supporting it."""

    value: str
    display: str
    claims: list[ClaimRecord] = field(default_factory=list)
    trust: TrustBreakdown | None = None

    @property
    def dsids(self) -> list[str]:
        return sorted({c.dsid for c in self.claims})

    @property
    def sources(self) -> list[str]:
        return sorted({c.source_type for c in self.claims})


@dataclass
class Conflict:
    subject: str
    predicate: Predicate
    versions: list[Version]
    best: Version | None
    margin: float
    reason: str

    @property
    def decided(self) -> bool:
        return self.best is not None

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "predicate": self.predicate.name,
            "mutable": self.predicate.mutable,
            "decided": self.decided,
            "supersession": self.predicate.mutable and self.decided,
            "reason": self.reason,
            "margin": round(self.margin, 3),
            "versions": [
                {
                    "value": v.display,
                    "documents": v.dsids,
                    "sources": v.sources,
                    "claims": len(v.claims),
                    "trust": v.trust.as_dict() if v.trust else None,
                    "best_supported": v is self.best,
                }
                for v in self.versions
            ],
        }


def _directness(record: ClaimRecord) -> float:
    """First-party statement versus a report of one.

    An email header or a meeting attendee line states a fact about its own
    participants; chat prose more often relays something heard elsewhere.
    """
    hearsay = ("said", "told", "heard", "apparently", "i think", "believe",
               "reportedly", "rumour", "rumor")
    quote = (record.quote or "").casefold()
    if any(marker in quote for marker in hearsay):
        return 0.3
    if record.source_type in ("gmail", "fireflies"):
        return 1.0
    return 0.7


def _corroboration(version: Version, total_documents: int) -> float:
    """Independent documents asserting this value, discounted for duplication.

    Copies of one another are not independent evidence. Identical quotes are
    counted once, which is the cheap half of PLAN.md's duplicate discounting;
    near-duplicate detection belongs with the DERIVED_FROM edges and is not
    done here.
    """
    distinct_quotes = {c.quote.strip().casefold() for c in version.claims if c.quote}
    independent = max(len(version.dsids), 1)
    if distinct_quotes and len(distinct_quotes) < independent:
        independent = len(distinct_quotes)
    return round(min(1.0, independent / max(total_documents, 2)), 3)


# How many competing versions must carry a stated date before recency is
# allowed to separate them. One is not enough: with a single dated version,
# "dated" becomes a synonym for "newer", and the undated versions lose to a
# document that never claimed to be later than anything.
MIN_DATED_VERSIONS = 2


def _recency(record: ClaimRecord, predicate: Predicate,
             ordering: dict[str, int], *, comparable: bool = True) -> float:
    """Position in time, but only where time can legitimately change the answer.

    For an immutable relation this is neutral for every version, so a later
    document cannot win on lateness alone. It is also neutral when too few
    versions carry a date to compare — see `MIN_DATED_VERSIONS`. Only 15 of 176
    claim-bearing documents in this corpus state one, so that is the common
    case rather than an edge case.
    """
    if not predicate.mutable or not comparable:
        return 0.5
    position = ordering.get(record.dsid)
    if position is None or len(ordering) < 2:
        return 0.5
    return round(position / (len(ordering) - 1), 3)


def group_key(dsid: str, subject: str, predicate: str,
              identity: Mapping[tuple[str, str], int] | None = None,
              ) -> tuple[str, str] | None:
    """The fact a claim is about, or None if it cannot be contested.

    This is the single definition of "the same fact", and it exists because
    having two was the bug — twice. Selecting which claims to re-adjudicate
    computed the key one way while adjudication computed it another, so the two
    disagreed about what counted as the same fact and the incremental pass
    silently missed pairs the full sweep found.

    First it went wrong on the predicate: selection compared the raw spelling,
    so `has job title` never reached `works as` — 73 edges. Then on the subject:
    selection compared the surface, so `S. Ratnaparkhi` never reached `Sam` even
    though the resolver had already decided they are one person.

    Both callers now ask this. A fact is identified by *who or what* it is about
    — the resolved identity where there is one, the name otherwise, since most
    subjects are not people — and by the *canonical* predicate, never the raw
    one.
    """
    if not subject or not predicate:
        return None
    alignment = align(predicate)
    if not alignment.aligned or not alignment.predicate.can_conflict:
        return None
    surface = subject.strip().casefold()
    entity = (identity or {}).get((dsid, surface))
    who = f"entity:{entity}" if entity is not None else f"name:{surface}"
    return (who, alignment.predicate.name)


def detect_conflicts(
    records: Iterable[ClaimRecord],
    *,
    document_order: Sequence[str] | None = None,
    subject_identity: Mapping[tuple[str, str], int] | None = None,
) -> tuple[list[Conflict], dict]:
    """Find contested facts and weigh their versions.

    `document_order` is oldest-first, and supplies recency where a corpus has no
    reliable per-document timestamp. Without it recency contributes nothing
    rather than being guessed.

    `subject_identity` maps `(dsid, casefolded subject)` to the entity the
    resolver decided that surface refers to. Supplying it is what stops a
    contested fact from being assembled out of two different people: grouping on
    the *name* alone put Anna Liu at cedarwave.com and Anna Liu at cloudwave.com
    into one dispute about one person's employer, and Elena Rossi at acmefin.com
    against Elena Rossi at elevate-ai.it. Thirty-one such edges were in the
    graph. A subject with no resolved identity still groups by name, because a
    name is the only handle there is — but where the graph knows who somebody
    is, that is what decides whether two claims are even about the same subject.
    """
    ordering = {dsid: i for i, dsid in enumerate(document_order or [])}
    identity = subject_identity or {}

    grouped: dict[tuple[str, str], list[ClaimRecord]] = defaultdict(list)
    unmapped: dict[str, int] = defaultdict(int)
    aligned_predicates: dict[str, Predicate] = {}
    # The group key is an identity, which is not printable; this carries a human
    # name for it so a conflict still reports whose fact is contested.
    display_subject: dict[str, str] = {}

    for record in records:
        alignment = align(record.predicate)
        if not alignment.aligned:
            unmapped[record.predicate.strip().casefold()] += 1
            continue
        # Only a single-valued relation can be contradicted. For everything
        # else, differing objects are simply more of the same fact.
        key = group_key(record.dsid, record.subject, record.predicate, identity)
        if key is None:
            continue
        predicate = alignment.predicate
        grouped[key].append(record)
        display_subject[key[0]] = record.subject.strip()
        aligned_predicates[predicate.name] = predicate

    conflicts: list[Conflict] = []
    for (subject_key, predicate_name), claims in grouped.items():
        subject = display_subject.get(subject_key, subject_key)
        predicate = aligned_predicates[predicate_name]
        by_value: dict[str, Version] = {}
        for claim in claims:
            value = normalise_object(claim.object)
            if not value:
                continue
            version = by_value.setdefault(
                value, Version(value=value, display=claim.object.strip()))
            version.claims.append(claim)

        if len(by_value) < 2:
            continue

        total_documents = len({c.dsid for c in claims})
        # Recency may only separate versions that can actually be compared on
        # it. A dated version beating undated ones is not evidence of being
        # later, only of being dated.
        dated_versions = sum(
            1 for v in by_value.values()
            if any(c.dsid in ordering for c in v.claims))
        comparable = dated_versions >= MIN_DATED_VERSIONS
        for version in by_value.values():
            authority = max(
                source_authority(predicate, c.source_type) for c in version.claims)
            directness = max(_directness(c) for c in version.claims)
            recency = max(_recency(c, predicate, ordering, comparable=comparable)
                          for c in version.claims)
            version.trust = TrustBreakdown(
                authority=authority,
                corroboration=_corroboration(version, total_documents),
                directness=round(directness, 3),
                recency=recency,
                mutable=predicate.mutable,
            )

        # System-of-record gate, applied before the weighted score.
        #
        # Authority is not really one component among several. If a relation has
        # a declared system of record — Jira for a ticket's status, email for a
        # person's own address — and exactly one version comes from it, that
        # settles the matter whatever the other components say. Expressed purely
        # as a weight it does not: a later passing mention in chat can out-score
        # the tracker on recency by a hair, and the result reads as a coin toss
        # between an authoritative record and a rumour.
        of_record = [
            v for v in by_value.values()
            if any(c.source_type in predicate.authority for c in v.claims)
        ]
        if predicate.authority and len(of_record) == 1:
            best = of_record[0]
            for version in by_value.values():
                version.trust = version.trust  # already computed above
            others = sorted(
                (v for v in by_value.values() if v is not best),
                key=lambda v: -v.trust.score)
            source = next(c.source_type for c in best.claims
                          if c.source_type in predicate.authority)
            conflicts.append(Conflict(
                subject=claims[0].subject.strip(), predicate=predicate,
                versions=[best, *others], best=best,
                margin=best.trust.score - others[0].trust.score,
                reason=(
                    f"{best.display!r} comes from {source}, the system of record "
                    f"for {predicate.name}; the other versions do not"
                ),
            ))
            continue

        versions = sorted(by_value.values(), key=lambda v: -v.trust.score)
        best, runner_up = versions[0], versions[1]
        margin = best.trust.score - runner_up.trust.score

        if margin >= DECISIVE_MARGIN:
            if predicate.mutable and best.trust.recency > runner_up.trust.recency:
                # A later statement about a changeable fact is normally not a
                # rival version but the current one. Saying so is more useful
                # than declaring the older claim wrong, and it is what the
                # SUPERSEDES relation records.
                reason = (
                    f"{best.display!r} is the most recent stated value; the "
                    f"others are earlier and read as superseded"
                )
            else:
                reason = (
                    f"{best.display!r} leads on "
                    f"{_leading_component(best.trust, runner_up.trust)}"
                )
            decided = best
        else:
            reason = (
                f"{len(versions)} versions are within {margin:.3f} of one another; "
                "no version is well enough supported to call current"
            )
            decided = None

        conflicts.append(Conflict(
            subject=claims[0].subject.strip(), predicate=predicate,
            versions=versions, best=decided, margin=margin, reason=reason,
        ))

    conflicts.sort(key=lambda c: -len(c.versions))
    stats = {
        "groups_examined": len(grouped),
        "conflicts_found": len(conflicts),
        "decided": sum(1 for c in conflicts if c.decided),
        "unmapped_predicates": len(unmapped),
        "top_unmapped": sorted(unmapped.items(), key=lambda kv: -kv[1])[:8],
    }
    return conflicts, stats


def _leading_component(best: TrustBreakdown, other: TrustBreakdown) -> str:
    """Name the component that actually decided it, for the explanation."""
    w = best.weights
    deltas = {
        "source authority": (best.authority - other.authority) * w["authority"],
        "corroboration": (best.corroboration - other.corroboration) * w["corroboration"],
        "directness": (best.directness - other.directness) * w["directness"],
        "recency": (best.recency - other.recency) * w["recency"],
    }
    name, value = max(deltas.items(), key=lambda kv: kv[1])
    return name if value > 0 else "a combination of components"
