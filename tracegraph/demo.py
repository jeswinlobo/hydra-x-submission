"""The demo questions, in one place, with what each is expected to return.

They lived in two places and drifted: the CLI advertised three questions the
stability check had never run, one of which the corpus does not actually answer.
Anybody following the README would have seen an abstention where a supported
answer was promised, and the ten-round check would have gone on passing, because
it was checking different questions.

So the list is defined once and imported by both. A question added here is a
question the stability check will hold to its expected answerability before
anyone records anything.

The last two are not there to succeed. One lands on a fact the corpus contradicts
itself about, and one is unanswerable outright — between them they cover the two
behaviours the track brief asks for by name and that a confident-sounding system
gets wrong: conflict resolution, and knowing when the answer is absent.
"""

from __future__ import annotations

SUPPORTED = "supported"
INSUFFICIENT = "insufficient"
CONFLICTING = "conflicting"

# Each entry lists the verdicts that are *acceptable*, not one that is required.
# Whether an answer is `conflicting` depends on whether the prose the model wrote
# states a value another document disputes, so a question touching a contested
# area is legitimately either. The invariants the stability check enforces —
# an abstention cites nothing, a conflicting verdict names the rival version, a
# supported answer is not sitting on an unreported dispute — do not vary.
DEMO_QUESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Which quantization profile caused the P95 latency regression?", (SUPPORTED,)),
    ("What did Redwood commit to for SOC 2 and audit evidence?",
     (SUPPORTED, CONFLICTING)),
    ("Interview slate and role anchors for the Staff Inference Engineer opening",
     (SUPPORTED, CONFLICTING, INSUFFICIENT)),
    ("What is the Q4 2029 revenue target for the Antarctic division?", (INSUFFICIENT,)),
)

QUESTIONS: tuple[str, ...] = tuple(q for q, _ in DEMO_QUESTIONS)
