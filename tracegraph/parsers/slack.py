"""Slack parser — bare handles, which is where resolution gets hard.

`title` is the channel. `content` is speaker-prefixed lines:

    sasha: Heads up — we started seeing a 2.5-3x increase in p95/p99 latency ...
    kevin: Thanks — any noisy neighbor alerts? GPU memory pressure?

Handles are lowercase, usually a bare first name, with no surname and no domain.
Nothing in a Slack document identifies who `sasha` is; that link comes from
email headers and co-occurrence elsewhere in the corpus, which is exactly the
problem the track poses.

Fenced code blocks carry log output full of colons and must not be read as
speech.
"""

from __future__ import annotations

import re

from .base import (
    PERSON,
    ROLE_SPEAKER,
    Mention,
    ParsedDoc,
    _split_code_fences,
    extract_references,
    in_spans,
)

SOURCE_TYPE = "slack"

# A speaker line starts at the beginning of a line with a short handle followed
# by a colon and a space. The handle character class excludes spaces, so prose
# like "Note: this is fine" cannot match, and the length bound keeps a sentence
# fragment ending in a colon out.
_SPEAKER_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{1,30}):[ \t]", re.MULTILINE)

# Slack-style explicit mentions inside message text: <@handle> or @handle.
_AT_MENTION_RE = re.compile(r"<?@([a-z0-9][a-z0-9._-]{1,30})>?")


def parse(doc_id: str, title: str, content: str) -> ParsedDoc:
    doc = ParsedDoc(doc_id=doc_id, source_type=SOURCE_TYPE, title=title)

    # The channel is an entity in its own right — shared membership is one of the
    # graph signals that resolves an ambiguous handle — but it comes from the
    # title, not the body, so it is not a Mention. A Mention carries offsets into
    # `content` and must reproduce its surface from them; the channel has no such
    # offsets. It reaches the graph as a Channel entity plus a SENT_IN edge built
    # from this attribute.
    channel = title.strip()
    if channel:
        doc.attributes["channel"] = channel

    fences = _split_code_fences(content)

    for match in _SPEAKER_RE.finditer(content):
        if in_spans(match.start(), fences):
            continue
        handle = match.group(1)
        doc.mentions.append(
            Mention(
                surface=match.group(1),
                kind=PERSON,
                role=ROLE_SPEAKER,
                start=match.start(1),
                end=match.end(1),
                attributes={"handle": handle.casefold(), "channel": channel},
            )
        )

    for match in _AT_MENTION_RE.finditer(content):
        if in_spans(match.start(), fences):
            continue
        handle = match.group(1)
        doc.mentions.append(
            Mention(
                surface=match.group(1),
                kind=PERSON,
                role="at_mention",
                start=match.start(1),
                end=match.end(1),
                attributes={"handle": handle.casefold(), "channel": channel},
            )
        )

    doc.references = extract_references(content)
    return doc
