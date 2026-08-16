"""Fireflies parser — meeting attendees with organisational affiliation.

The second strong identity source after email, and the only one that states
which organisation a person belongs to:

    Meeting Header:
    Date: 2025-03-27
    Time: 15:00 UTC
    Duration: 62 minutes
    Attendees: Maya Patel (Redwood AE); Jonas Reed (Redwood SE); Sofia Alvarez (Tethys CTO)

Attendees are semicolon-separated, each `Name (Org Role)`. Affiliation is what
lets resolution apply a cannot-link rule: two people with similar names at
different organisations are not the same person.
"""

from __future__ import annotations

import re

from .base import (
    PERSON,
    ROLE_ATTENDEE,
    Mention,
    ParsedDoc,
    extract_references,
)

SOURCE_TYPE = "fireflies"

_ATTENDEES_RE = re.compile(r"^Attendees:[ \t]*(.+)$", re.MULTILINE)
_HEADER_FIELD_RE = re.compile(
    r"^(Date|Time|Duration):[ \t]*(.+)$", re.MULTILINE
)
# `Maya Patel (Redwood AE)` — the parenthetical is optional, since some
# transcripts list a bare name.
_ATTENDEE_RE = re.compile(r"\s*([^;()]+?)\s*(?:\(([^)]*)\))?\s*(?:;|$)")


def parse(doc_id: str, title: str, content: str) -> ParsedDoc:
    doc = ParsedDoc(doc_id=doc_id, source_type=SOURCE_TYPE, title=title)

    for match in _HEADER_FIELD_RE.finditer(content):
        doc.attributes[match.group(1).lower()] = match.group(2).strip()

    attendees = _ATTENDEES_RE.search(content)
    if attendees is None:
        doc.references = extract_references(content)
        return doc

    line = attendees.group(1)
    line_start = attendees.start(1)

    for match in _ATTENDEE_RE.finditer(line):
        name = match.group(1)
        if not name or not name.strip():
            continue
        # A truncated attendee list ends mid-name ("Sofia Alv..."); keeping it
        # would create an entity that never appears anywhere else.
        if name.rstrip().endswith("..."):
            continue

        start = line_start + match.start(1)
        attributes: dict[str, str] = {}
        parenthetical = (match.group(2) or "").strip()
        if parenthetical:
            attributes["affiliation"] = parenthetical
            # The first token is conventionally the organisation, the rest the
            # role. Recorded separately rather than parsed further, because the
            # convention is not reliable enough to split on confidently.
            org, _, role = parenthetical.partition(" ")
            if org:
                attributes["organisation"] = org
            if role:
                attributes["role"] = role

        doc.mentions.append(
            Mention(
                surface=line[match.start(1) : match.end(1)],
                kind=PERSON,
                role=ROLE_ATTENDEE,
                start=start,
                end=start + (match.end(1) - match.start(1)),
                attributes=attributes,
            )
        )

    # Organisations named in the attendee line are entities too; they carry the
    # customer-versus-internal distinction the conflict logic needs. Joined into
    # a scalar because graph properties cannot hold lists.
    seen_orgs = sorted(
        {m.attributes["organisation"] for m in doc.mentions
         if "organisation" in m.attributes}
    )
    if seen_orgs:
        doc.attributes["organisations"] = ";".join(seen_orgs)

    doc.references = extract_references(content)
    return doc
