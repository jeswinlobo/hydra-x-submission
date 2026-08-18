#!/usr/bin/env python
"""Retrieval quality at each evidence budget, so the reported number is the one run.

`scripts/75_retrieval_eval.py` measures one budget per invocation, and the
budget it defaults to (20) is not the budget the system runs:
`AnswerController.max_documents` is 8, and everything downstream — enrichment,
claim extraction, synthesis — sees that prefix and nothing wider. Quoting top-20
recall beside a top-8 answer path describes a configuration that does not exist.

This measures 4, 8 and 20 together from a single pass. bm25 ordering is fixed
for a given query, so the top-k list is a prefix of the top-20 list and recall at
k is recoverable from it. That matters practically: `fts.search` opens a fresh
connection against a 2.5 GB index per call, so three sweeps would cost three
times what one does and could only agree with itself.

The gold firewall of the sibling script is preserved exactly. `read_questions`
supplies question text and cannot return an answer key; `read_answer_key`
supplies the key and cannot return the question. Only ids cross between them, and
only `question` reaches the retriever.

    uv run python scripts/76_recall_by_budget.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config, fts  # noqa: E402
from tracegraph.ids import node_id  # noqa: E402
from tracegraph.parquet_reader import read_answer_key, read_questions  # noqa: E402

BUDGETS = (4, 8, 20)


def load_questions(limit: int) -> list[dict]:
    """Question text, joined to its answer key on question_id.

    Two reads through two narrow doors, as in `75_retrieval_eval.py`: the wide
    read that would pull `gold_answer` and `answer_facts` into memory is the one
    thing this file must not do.
    """
    key = {row["question_id"]: list(row["expected_doc_ids"] or [])
           for row in read_answer_key()}
    out = []
    for row in read_questions():
        expected = key.get(row["question_id"]) or []
        if not expected:
            continue
        out.append({
            "question_id": row["question_id"],
            "question": row["question"],
            "question_type": row["question_type"],
            "expected": expected,
        })
        if limit and len(out) >= limit:
            break
    return out


def median(values: list[int]) -> int | None:
    """Upper median, matching `75_retrieval_eval.py`'s `ranked[len // 2]`."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def score(rows: list[dict], k: int) -> dict:
    """Every metric at one budget, from the top-20 retrieval already in hand."""
    per_type: dict[str, list[float]] = defaultdict(list)
    recalls, ranks = [], []
    any_hit = full = 0

    for row in rows:
        expected = set(row["expected"])
        retrieved = row["retrieved"][:k]
        found = expected & set(retrieved)
        recall = len(found) / len(expected)
        recalls.append(recall)
        per_type[row["question_type"]].append(recall)
        if found:
            any_hit += 1
            ranks.append(
                next(i + 1 for i, d in enumerate(retrieved) if d in expected)
            )
        if recall == 1.0:
            full += 1

    total = len(rows)
    return {
        "k": k,
        "questions": total,
        "mean_recall": sum(recalls) / max(total, 1),
        "any_hit": any_hit,
        "any_hit_pct": any_hit / max(total, 1),
        "full_recall": full,
        "full_recall_pct": full / max(total, 1),
        "median_first_rank": median(ranks),
        "ranked_questions": len(ranks),
        "by_type": {
            qtype: {"n": len(v), "mean_recall": sum(v) / len(v)}
            for qtype, v in per_type.items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="0 means every question")
    ap.add_argument("--out", default="artifacts/recall_by_budget.json")
    args = ap.parse_args()

    widest = max(BUDGETS)
    questions = load_questions(args.limit)
    print(f"{len(questions)} questions with an answer key; "
          f"one search each at limit {widest}, scored at {list(BUDGETS)}\n")

    # The index rowid is the document's deterministic graph id, so a hit maps
    # back to a dsid without a second lookup table. Column-projected and
    # batch-at-a-time: this map covers the whole corpus.
    id_to_dsid: dict[int, str] = {}
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    for batch in parquet.iter_batches(batch_size=8000, columns=["doc_id"]):
        for dsid in batch.to_pydict()["doc_id"]:
            id_to_dsid[node_id("Document", dsid)] = dsid
    print(f"resolved {len(id_to_dsid):,} document ids\n")

    rows, latencies = [], []
    for index, q in enumerate(questions, 1):
        started = time.perf_counter()
        hits = fts.search(q["question"], limit=widest)
        latencies.append((time.perf_counter() - started) * 1000)
        rows.append({
            "question_id": q["question_id"],
            "question_type": q["question_type"],
            "expected": q["expected"],
            "retrieved": [id_to_dsid.get(node, "") for node, _ in hits],
        })
        if index % 25 == 0:
            print(f"  {index}/{len(questions)} searched", flush=True)

    budgets = {str(k): score(rows, k) for k in BUDGETS}

    latencies.sort()
    print(f"\nretrieval latency p50 {latencies[len(latencies) // 2]:.1f}ms "
          f"p95 {latencies[int(len(latencies) * 0.95)]:.1f}ms\n")

    for k in BUDGETS:
        s = budgets[str(k)]
        total = s["questions"]
        print(f"k={k} over {total} questions")
        print(f"  document recall (mean)   {s['mean_recall']:.3f}")
        print(f"  at least one expected    {s['any_hit']}/{total} "
              f"({s['any_hit_pct']:.1%})")
        print(f"  every expected document  {s['full_recall']}/{total} "
              f"({s['full_recall_pct']:.1%})")
        print(f"  median rank of first hit {s['median_first_rank']} "
              f"(over the {s['ranked_questions']} questions with a hit)")
        for qtype, stats in sorted(
            s["by_type"].items(), key=lambda kv: -kv[1]["mean_recall"]
        ):
            print(f"    {qtype[:34]:36} n={stats['n']:>3}  "
                  f"recall={stats['mean_recall']:.3f}")
        print()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "note": "Recorded run of scripts/76_recall_by_budget.py over the full "
                "corpus. One search per question at limit 20; smaller budgets "
                "are prefixes of that ranking. Per-question rows are omitted.",
        "searched_limit": widest,
        "questions": len(rows),
        "latency_ms": {
            "p50": latencies[len(latencies) // 2],
            "p95": latencies[int(len(latencies) * 0.95)],
        },
        "budgets": budgets,
    }, indent=2))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
