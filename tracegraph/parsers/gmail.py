"""Gmail parser — the identity backbone of the corpus.

Over half the corpus is email, and the headers are the one place where a display
name and an email address appear together, stated by the document rather than
inferred. Every `(name, email)` pair extracted here is a strong key for entity
resolution that owes nothing to the quarantined identity oracle.

Header shape, verified against real documents:

    From: Alyssa Chen <alyssa.chen@cascadefg.com>
    To: Markus Klein <markus.klein@redwoodinference.com>
    Cc: Tom Becker <tom.becker@cascadefg.com>, Rachel Kim <rachel.kim@redwoodinference.com>
    Date: Tue, Jun 3, 2025 at 9:12 AM
    Subject: Escalation: rollback guarantees ...
"""

from __future__ import annotations

import re

from .base import (
    NAME_EMAIL_RE,
    BARE_EMAIL_RE,
    PERSON,
    ROLE_EMAIL_CC,
    ROLE_EMAIL_FROM,
    ROLE_EMAIL_TO,
    Mention,
    ParsedDoc,
    email_domain,
    extract_references,
)

SOURCE_TYPE = "gmail"

# Only the leading header block is trusted. Quoted reply chains further down
# repeat headers from earlier messages, and attributing those to this document
# would invent participation that did not happen here.
_HEADER_LINE_RE = re.compile(
    r"^(From|To|Cc|Bcc|Date|Subject):[ \t]*(.*)$", re.MULTILINE
)

_ROLE_BY_HEADER = {
    "from": ROLE_EMAIL_FROM,
    "to": ROLE_EMAIL_TO,
    "cc": ROLE_EMAIL_CC,
    "bcc": ROLE_EMAIL_CC,
}


def _header_block_end(content: str) -> int:
    """End of the leading header block: the first blank line after a header.

    Everything past it is body, including quoted replies whose headers belong to
    other messages.
    """
    first = _HEADER_LINE_RE.search(content)
    if first is None:
        return 0
    blank = content.find("\n\n", first.end())
    return len(content) if blank == -1 else blank


def parse(doc_id: str, title: str, content: str) -> ParsedDoc:
    doc = ParsedDoc(doc_id=doc_id, source_type=SOURCE_TYPE, title=title)
    header_end = _header_block_end(content)

    for match in _HEADER_LINE_RE.finditer(content):
        if match.start() >= header_end:
            break
        header = match.group(1).lower()
        value_start = match.start(2)
        value = match.group(2)

        if header in ("date", "subject"):
            if value.strip():
                doc.attributes[header] = value.strip()
            continue

        role = _ROLE_BY_HEADER[header]

        # `Name <addr>` pairs first; they carry the most information.
        claimed: list[tuple[int, int]] = []
        for pair in NAME_EMAIL_RE.finditer(value):
            raw = pair.group(1)
            # A comma-separated recipient list leaves leading whitespace on
            # every name after the first. Advance the offsets alongside the trim
            # so the surface still reproduces from them exactly.
            lead = len(raw) - len(raw.lstrip(' \t"'))
            name = raw.strip().strip('"')
            if not name:
                continue
            email = pair.group(2).strip().lower()
            start = value_start + pair.start(1) + lead
            doc.mentions.append(
                Mention(
                    surface=name,
                    kind=PERSON,
                    role=role,
                    start=start,
                    end=start + len(name),
                    attributes={"email": email, "domain": email_domain(email)},
                )
            )
            claimed.append((pair.start(), pair.end()))

        # Addresses with no display name still identify a person.
        for bare in BARE_EMAIL_RE.finditer(value):
            if any(s <= bare.start() < e for s, e in claimed):
                continue
            email = bare.group(1).lower()
            doc.mentions.append(
                Mention(
                    surface=bare.group(1),
                    kind=PERSON,
                    role=role,
                    start=value_start + bare.start(1),
                    end=value_start + bare.end(1),
                    attributes={"email": email, "domain": email_domain(email)},
                )
            )

    doc.references = extract_references(content)
    return doc
