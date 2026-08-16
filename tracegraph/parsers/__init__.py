"""Source-aware parsers.

Dispatch is by `source_type`. Sources with a verifiable identity structure get a
dedicated parser; everything else falls through to `generic`, which extracts
references and block structure and leaves identity to claim extraction.

See docs/source-notes.md for the document shapes these were written against.
"""

from __future__ import annotations

from . import fireflies, generic, gmail, slack
from .base import Mention, ParsedDoc, Reference

_DEDICATED = {
    gmail.SOURCE_TYPE: gmail.parse,
    slack.SOURCE_TYPE: slack.parse,
    fireflies.SOURCE_TYPE: fireflies.parse,
}


def parse_document(doc_id: str, source_type: str, title: str, content: str) -> ParsedDoc:
    """Parse one document with the parser registered for its source."""
    parser = _DEDICATED.get(source_type)
    if parser is not None:
        return parser(doc_id, title, content)
    return generic.parse(doc_id, source_type, title, content)


__all__ = ["Mention", "ParsedDoc", "Reference", "parse_document"]
