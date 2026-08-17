"""The demo questions, in one place, with what each is expected to return.

They lived in two places and drifted: the CLI advertised three questions the
stability check had never run, one of which the corpus does not actually answer.
Anybody following the README would have seen an abstention where a supported
answer was promised, and the ten-round check would have gone on passing, because
it was checking different questions.

So the list is defined once and imported by both. A question added here is a
question the stability check will hold to its expected answerability before
anyone records anything.

Four questions covering the four verdicts the brief asks for. Two are not there
to succeed: one lands on a fact the corpus contradicts itself about, and one is
unanswerable outright. Those are the behaviours a confident-sounding system gets
wrong, and they are the reason `scripts/80_demo_check.py` refuses to pass a run
in which no question came back `conflicting`.
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
    # Four documents give Grace O'Connor four different titles, so whenever this
    # is answered at all it is contested — eight rounds in ten. In the other two
    # the model declined to state a role, which is a legitimate outcome and not
    # something to pin a label against. What guarantees conflict detection is
    # still reaching the answer path is the run-level check that *some* round
    # came back contested, not a per-question label.
    ("What is Grace O'Connor's role at Redwood?", (CONFLICTING, INSUFFICIENT)),
    ("What did Redwood commit to for SOC 2 and audit evidence?",
     (SUPPORTED, CONFLICTING)),
    ("What is the Q4 2029 revenue target for the Antarctic division?", (INSUFFICIENT,)),
)

QUESTIONS: tuple[str, ...] = tuple(q for q, _ in DEMO_QUESTIONS)
