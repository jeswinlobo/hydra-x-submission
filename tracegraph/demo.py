"""The demo questions, in one place, with what each is expected to return.

They lived in two places and drifted: the CLI advertised three questions the
stability check had never run, one of which the corpus does not actually answer.
Anybody following the README would have seen an abstention where a supported
answer was promised, and the ten-round check would have gone on passing, because
it was checking different questions.

So the list is defined once and imported by both. A question added here is a
question the stability check will hold to its expected answerability before
anyone records anything.

The third is unanswerable on purpose. A demo that cannot show an honest refusal
is showing half the system.
"""

from __future__ import annotations

SUPPORTED = "supported"
INSUFFICIENT = "insufficient"

DEMO_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Which quantization profile caused the P95 latency regression?", SUPPORTED),
    ("What did Redwood commit to for SOC 2 and audit evidence?", SUPPORTED),
    ("What is the Q4 2029 revenue target for the Antarctic division?", INSUFFICIENT),
)

QUESTIONS: tuple[str, ...] = tuple(q for q, _ in DEMO_QUESTIONS)
