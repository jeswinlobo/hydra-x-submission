"""Shared parser vocabulary and the tier-1 extractors.

Tier 1 is everything that can be read out of a document exactly, without a
model: email headers, meeting attendee lines, speaker handles, ticket keys,
URLs. PLAN.md's rule is to prefer missing a weak edge over creating a false one,
so this tier is deliberately conservative — a pattern either matches
unambiguously or the extractor declines.

Every mention records the exact surface text and its character offsets in the
document, because an offset that cannot be re-derived from the source is not
provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# --- Vocabulary -------------------------------------------------------------

# Entity kinds. Kept as plain strings rather than an enum because they are
# written into graph properties and read back as strings.
PERSON = "person"
ORGANISATION = "organisation"
CHANNEL = "channel"
TICKET = "ticket"
ACCOUNT = "account"

# Mention roles describe how a surface appeared, which is what makes one piece
# of evidence stronger than another during resolution. A sender header is a
# far better identity signal than a name appearing in prose.
ROLE_EMAIL_FROM = "email_from"
ROLE_EMAIL_TO = "email_to"
ROLE_EMAIL_CC = "email_cc"
ROLE_SPEAKER = "speaker"
ROLE_ATTENDEE = "attendee"
ROLE_REFERENCE = "reference"


@dataclass(frozen=True)
class Mention:
    """One surface form of an entity, located exactly in a document."""

    surface: str
    kind: str
    role: str
    start: int
    end: int
    # Attributes that came with the mention rather than being inferred, such as
    # the email address on a From: line or the organisation in an attendee line.
    attributes: dict[str, str] = field(default_factory=dict)

    def verify(self, content: str) -> bool:
        """Offsets must reproduce the surface exactly, or the mention is a lie."""
        return content[self.start : self.end] == self.surface


@dataclass(frozen=True)
class Reference:
    """An exact, verifiable pointer from one document to another artefact."""

    target: str
    kind: str  # "ticket" | "url"
    start: int
    end: int


@dataclass
class ParsedDoc:
    doc_id: str
    source_type: str
    title: str
    mentions: list[Mention] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    # Structure the parser is confident about: channel name, thread key,
    # meeting date. Scalars only, since graph properties cannot hold lists.
    attributes: dict[str, str] = field(default_factory=dict)

    def verified_mentions(self, content: str) -> list[Mention]:
        return [m for m in self.mentions if m.verify(content)]


# --- Shared patterns --------------------------------------------------------

# Ticket keys: PROJ-123. Bounded to 2-10 uppercase letters so ordinary
# capitalised prose ("I-5", "COVID-19") does not slip through as a ticket.
TICKET_KEY_RE = re.compile(r"\b([A-Z]{2,10}-\d{1,6})\b")

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")

# A display name followed by a bracketed address: `Alyssa Chen <a.chen@x.com>`.
NAME_EMAIL_RE = re.compile(r"([^<>,;]+?)\s*<([^<>@\s]+@[^<>\s]+)>")

BARE_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def normalise_name(raw: str) -> str:
    """Collapse whitespace and casefold. Used as the alias lookup key."""
    return re.sub(r"\s+", " ", raw).strip().casefold()


def email_local_part(email: str) -> str:
    return email.split("@", 1)[0].casefold()


def email_domain(email: str) -> str:
    return email.split("@", 1)[1].casefold() if "@" in email else ""


def name_tokens(raw: str) -> set[str]:
    """Alphabetic tokens of a name or email local part, for alias bridging.

    `alyssa.chen` and `Alyssa Chen` both reduce to {alyssa, chen}, which is what
    lets a Slack handle be linked to an email address by evidence rather than by
    string equality. Single characters are dropped so a middle initial does not
    create a spurious shared token.
    """
    return {t for t in re.split(r"[^a-z]+", raw.casefold()) if len(t) > 1}


def extract_references(content: str) -> list[Reference]:
    """Ticket keys and URLs — exact pointers, no inference."""
    refs: list[Reference] = []
    for match in TICKET_KEY_RE.finditer(content):
        refs.append(
            Reference(target=match.group(1), kind="ticket",
                      start=match.start(1), end=match.end(1))
        )
    for match in URL_RE.finditer(content):
        refs.append(
            Reference(target=match.group(0).rstrip(".,);"), kind="url",
                      start=match.start(), end=match.end())
        )
    return refs


def _split_code_fences(content: str) -> list[tuple[int, int]]:
    """Spans of fenced code, so their contents are not read as prose.

    Slack transcripts carry log output containing colons, which would otherwise
    parse as speaker lines.
    """
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"```.*?```", content, re.DOTALL):
        spans.append((match.start(), match.end()))
    return spans


def in_spans(index: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)
