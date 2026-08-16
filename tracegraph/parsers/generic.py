"""Fallback parser for sources with no reliable identity structure.

GitHub, Linear, Jira, HubSpot, Confluence, and Google Drive share a `key:`
labelled-block layout (`description:`, `tasks:`, `summary:`, `notes:`,
`use_case_summary:`) wrapping free text. None of them state who wrote the
document in a form that can be trusted, so this parser extracts only what is
exact: the labelled blocks it can see, and cross-document references.

Identity for these sources comes later, from claim extraction, rather than being
guessed here. Inventing an author from prose would be exactly the false edge
PLAN.md rules out.
"""

from __future__ import annotations

import re

from .base import ParsedDoc, extract_references

# A block label is a lowercase identifier alone on a line ending in a colon.
_BLOCK_RE = re.compile(r"^([a-z][a-z0-9_]{2,30}):[ \t]*$", re.MULTILINE)

# Labels worth recording as document structure. Anything else is left in the
# body for claim extraction.
_KNOWN_BLOCKS = {
    "description",
    "tasks",
    "summary",
    "transcript",
    "notes",
    "use_case_summary",
}


def parse(doc_id: str, source_type: str, title: str, content: str) -> ParsedDoc:
    doc = ParsedDoc(doc_id=doc_id, source_type=source_type, title=title)

    labels = [
        (m.group(1), m.end())
        for m in _BLOCK_RE.finditer(content)
        if m.group(1) in _KNOWN_BLOCKS
    ]
    for index, (label, body_start) in enumerate(labels):
        body_end = (
            labels[index + 1][1] - len(labels[index + 1][0]) - 2
            if index + 1 < len(labels)
            else len(content)
        )
        body = content[body_start:body_end].strip()
        if body:
            # Only the presence and extent of a block is structure; the text
            # itself stays in Parquet rather than being copied into the graph.
            doc.attributes[f"block_{label}_chars"] = str(len(body))

    if labels:
        doc.attributes["blocks"] = ";".join(label for label, _ in labels)

    doc.references = extract_references(content)
    return doc
