#!/usr/bin/env python
"""Score abstention against the benchmark's own unanswerable questions, with
and without the graph.

PLAN.md called for a graph-vs-no-graph ablation and only the lexical baseline
was ever measured. The two attempts recorded in `artifacts/seed_ablation.json`
failed for a reason worth restating: whether synthesis *phrases* an answer one
way or another is not stable run to run, so a single-run A/B on answer text
measures the model's temperature. Four verdicts in twelve moved between two runs
of identical code.

Abstention is the one place that objection does not apply with the same force.
It is a binary, it is the capability the track brief names ("saying 'not in the
data' instead of inventing an answer"), and the benchmark ships twenty questions
built to be unanswerable from the corpus. So it is measurable where answer
quality was not.

**Two arms, one variable.**

Both arms call `AnswerController.retrieve_documents`, so retrieval is held
identical — the same FTS query, the same ranking, the same eight documents.
They differ only in what is handed to the model:

* `graph`   — claim spans read out of HydraDB, each re-checked verbatim against
              its source body, plus the controller's citation validation and
              conflict walk. This is production.
* `nograph` — the raw text of those same documents, straight to the same model
              with the same system prompt and the same schema. No graph, no
              claims, no span validation. This is what the system would be if
              HydraDB were removed and the retriever wired directly to
              synthesis.

**Two question sets, because abstention alone is trivially gamed.** A system
that abstains always scores perfectly on unanswerable questions and is useless.
So the same arms also run answerable questions, and the false-abstention rate on
those is reported beside the true-abstention rate. Neither number means anything
without the other.

**No answer key is read.** `question_type` is in
`config.QUESTION_COLUMNS_ALLOWED`; `gold_answer` and `answer_facts` are not, and
this script never asks for them. The label *is* the type: `info_not_found`
questions are unanswerable by construction. So this scores itself through the
same narrow door the answering path uses, and `read_answer_key` is not involved
at all.

    uv run python scripts/78_abstention_eval.py
    uv run python scripts/78_abstention_eval.py --limit 5 --arms graph
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph import config  # noqa: E402
from tracegraph.controller import AnswerController  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ingest import OnDemandIngestor  # noqa: E402
from tracegraph.llm import Evidence, synthesise_answer  # noqa: E402
from tracegraph.parquet_reader import read_questions  # noqa: E402

# The benchmark's own label for a question the corpus cannot answer. Twenty of
# them, and every one carries no expected documents — which is why the retrieval
# eval had to skip them and why they were still unmeasured.
UNANSWERABLE_TYPE = "info_not_found"

# The control set. `basic` is the largest answerable category (175 questions)
# and the least equivocal: if a system abstains on these it is not being
# careful, it is broken. `high_level` is excluded deliberately — those carry no
# expected documents either, and a broad synthesis question is a genuinely
# arguable abstention, so scoring it either way would be unfair to both arms.
ANSWERABLE_TYPE = "basic"

# Fixed, so a rerun samples the same control questions and the two arms are
# never compared across different question sets.
SAMPLE_SEED = 20260820


def load_questions(limit: int) -> tuple[list[dict], list[dict]]:
    """The two question sets, through the door that cannot see an answer."""
    unanswerable: list[dict] = []
    answerable: list[dict] = []
    for row in read_questions(columns=("question_id", "question", "question_type")):
        if row["question_type"] == UNANSWERABLE_TYPE:
            unanswerable.append(row)
        elif row["question_type"] == ANSWERABLE_TYPE:
            answerable.append(row)

    # Match the control set to the unanswerable set in size, so a percentage
    # from one is comparable with a percentage from the other.
    random.Random(SAMPLE_SEED).shuffle(answerable)
    answerable = answerable[: len(unanswerable)]

    if limit:
        unanswerable = unanswerable[:limit]
        answerable = answerable[:limit]
    return unanswerable, answerable


def ask_graph(controller: AnswerController, question: str) -> tuple[str, int]:
    """Production: graph evidence, span re-checks, citation validation."""
    result = controller.answer(question, bodies={})
    contract = result.to_contract()
    return contract["answerability"], len(contract["document_ids"])


def ask_nograph(
    controller: AnswerController, ingestor: OnDemandIngestor, question: str
) -> tuple[str, int]:
    """The same documents, raw, straight to the same model.

    Deliberately *not* a strawman. It gets the identical retrieval, the identical
    model, the identical system prompt — which already instructs the model to
    abstain when the passages fall short — and the identical response schema. It
    is missing exactly one thing: the graph, and everything the controller can
    check because of it.
    """
    candidates = controller.retrieve_documents(question)
    evidence: list[Evidence] = []
    for candidate in candidates:
        body = ingestor.body(candidate["dsid"])
        if not body:
            continue
        # Bounded so a long document cannot crowd the window; the graph arm is
        # bounded too, by evidence_window over spans.
        evidence.append(
            Evidence(dsid=candidate["dsid"], text=body[:6000], title=candidate.get("title"))
        )

    # An empty retrieval is an abstention in both arms, and for the same reason:
    # there is nothing to answer from. Asking the model to answer from nothing
    # is what `synthesise_answer` refuses to do.
    if not evidence:
        return "insufficient", 0

    result = synthesise_answer(question, evidence[: controller.evidence_window])
    if not result.sufficient or not result.citations:
        return "insufficient", 0
    return "supported", len(set(result.citations))


def run_arm(
    arm: str,
    questions: list[dict],
    expect_abstention: bool,
    client: HydraClient,
    run_id: str,
    ingestor: OnDemandIngestor,
) -> dict:
    rows: list[dict] = []
    latencies: list[float] = []
    for index, row in enumerate(questions, start=1):
        controller = AnswerController(client, run_id, ingestor=ingestor)
        started = time.perf_counter()
        try:
            if arm == "graph":
                verdict, citations = ask_graph(controller, row["question"])
            else:
                verdict, citations = ask_nograph(controller, ingestor, row["question"])
            error = None
        except Exception as exc:  # noqa: BLE001 - a crash is a result, not a stop
            verdict, citations, error = "error", 0, f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)

        abstained = verdict == "insufficient"
        correct = abstained if expect_abstention else not abstained
        rows.append({
            "question_id": row["question_id"],
            "question": row["question"][:160],
            "verdict": verdict,
            "citations": citations,
            "abstained": abstained,
            "correct": correct,
            "seconds": round(elapsed, 2),
            "error": error,
        })
        mark = "." if correct else "X"
        print(f"  {arm:<8} {index:>3}/{len(questions)} {mark} {verdict}", flush=True)

    scored = [r for r in rows if r["verdict"] != "error"]
    abstentions = sum(1 for r in scored if r["abstained"])
    return {
        "questions": len(rows),
        "scored": len(scored),
        "errors": len(rows) - len(scored),
        "abstentions": abstentions,
        "abstention_rate": round(abstentions / len(scored), 4) if scored else None,
        "correct": sum(1 for r in scored if r["correct"]),
        "accuracy": round(
            sum(1 for r in scored if r["correct"]) / len(scored), 4) if scored else None,
        "latency_seconds": {
            "p50": round(statistics.median(latencies), 2) if latencies else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap each question set (0 = all twenty)")
    ap.add_argument("--arms", default="graph,nograph",
                    help="comma-separated: graph, nograph")
    ap.add_argument("--out", default="artifacts/abstention_eval.json")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    # Captured before the run, not after. A pass over forty questions takes long
    # enough that the tree can move underneath it, and stamping the commit at
    # the end labels results produced by older code with newer code's hash —
    # worse than no provenance, because it reads as a measurement of something
    # it did not measure. Whether the tree was dirty is recorded too: a clean
    # hash on a modified tree is the same lie in smaller print.
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip() or None
    dirty = bool(subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip())
    if len(arms) < 2:
        print("NOTE: fewer than two arms - this is a smoke run, not a "
              "graph-vs-no-graph comparison, and is recorded as such.\n")
    unanswerable, answerable = load_questions(args.limit)
    print(f"{len(unanswerable)} unanswerable ({UNANSWERABLE_TYPE}), "
          f"{len(answerable)} answerable ({ANSWERABLE_TYPE}); arms: {', '.join(arms)}\n")

    with HydraClient() as client:
        client.verify()
        rows = client.bolt_read(
            "MATCH (d:Document) RETURN d.run_id AS r ORDER BY r DESC LIMIT 1")
        run_id = rows[0]["r"] if rows else "ondemand"
        probe = client.http_query("MATCH (d:Document) RETURN count(*) AS c")
        ingestor = OnDemandIngestor(client, run_id)

        results: dict[str, dict] = {}
        for arm in arms:
            results[arm] = {}
            for label, questions, expect in (
                ("unanswerable", unanswerable, True),
                ("answerable", answerable, False),
            ):
                print(f"{arm} / {label}")
                results[arm][label] = run_arm(
                    arm, questions, expect, client, run_id, ingestor)
                print()

    # The headline pair. Reported together on purpose: a system that abstains on
    # everything scores 1.0 on the left and 0.0 on the right, and the point of
    # the control set is that this is visible rather than flattering.
    summary = {}
    for arm, blocks in results.items():
        un = blocks.get("unanswerable", {})
        an = blocks.get("answerable", {})
        summary[arm] = {
            "correct_abstention_rate": un.get("abstention_rate"),
            "false_abstention_rate": an.get("abstention_rate"),
            "balanced_accuracy": round(
                ((un.get("accuracy") or 0) + (an.get("accuracy") or 0)) / 2, 4)
            if un.get("accuracy") is not None and an.get("accuracy") is not None
            else None,
        }

    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "tree_dirty_at_start": dirty,
        # A single arm cannot answer "does the graph help"; saying so in the
        # artifact stops it being quoted as though it had.
        "comparison": len(arms) >= 2,
        "synthesis_model": config.SYNTHESIS_MODEL,
        "extraction_model": config.EXTRACTION_MODEL,
        "run_id": run_id,
        "read_epoch": probe.read_epoch,
        "question_sets": {
            "unanswerable": {"type": UNANSWERABLE_TYPE, "n": len(unanswerable)},
            "answerable": {"type": ANSWERABLE_TYPE, "n": len(answerable),
                           "sample_seed": SAMPLE_SEED},
        },
        "summary": summary,
        "arms": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    print("=" * 68)
    for arm, s in summary.items():
        print(f"{arm:<8} correct abstention {s['correct_abstention_rate']}  "
              f"false abstention {s['false_abstention_rate']}  "
              f"balanced {s['balanced_accuracy']}")
    print("=" * 68)
    print(f"\nrecorded -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
