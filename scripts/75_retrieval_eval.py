#!/usr/bin/env python
"""Measure retrieval against the benchmark's 500 questions, at full corpus scale.

This is the floor the rest of the system stands on. If retrieval never surfaces
the document an answer lives in, no amount of graph reasoning downstream can
recover it, and a headline number that skips this step is measuring the wrong
thing.

`expected_doc_ids` is gold, and this is evaluation, which is the one place it may
be read. It reaches no part of ingestion or answering: the retriever is handed
the question text and nothing else.

Reported per question type as well as overall, because a mean over ten
categories hides which of them the system cannot serve.

    uv run python scripts/75_retrieval_eval.py --limit 500 --k 20
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


def load_questions(limit: int) -> list[dict]:
    """Question text, joined to its answer key on question_id.

    Deliberately two reads through two narrow doors rather than one wide one.
    This used to be `pq.ParquetFile(...).read()`, which materialised every
    column — `gold_answer` and `answer_facts` included, neither of which this
    script has any use for — and made the firewall the docs describe a
    convention rather than a mechanism.

    `read_questions` cannot return gold; `read_answer_key` cannot return
    anything else. Only the ids and the expected documents cross between them,
    and only `question` reaches the retriever below.
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--k", type=int, default=20, help="documents retrieved per question")
    ap.add_argument("--out", default="artifacts/retrieval_eval.json")
    args = ap.parse_args()

    questions = load_questions(args.limit)
    print(f"{len(questions)} questions with an answer key, retrieving top {args.k}\n")

    # The index rowid is the document's deterministic graph id, so a hit maps
    # back to a dsid without a second lookup table.
    # Column-projected and batch-at-a-time rather than through
    # `iter_documents`, which materialises a dict per document — half a million
    # of them here, since this map covers the whole corpus. The gold firewall
    # governs the questions file; the documents file has no answer key to leak.
    id_to_dsid: dict[int, str] = {}
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    for batch in parquet.iter_batches(batch_size=8000, columns=["doc_id"]):
        for dsid in batch.to_pydict()["doc_id"]:
            id_to_dsid[node_id("Document", dsid)] = dsid
    print(f"resolved {len(id_to_dsid):,} document ids\n")

    per_type: dict[str, list] = defaultdict(list)
    rows, latencies = [], []
    for q in questions:
        started = time.perf_counter()
        hits = fts.search(q["question"], limit=args.k)
        latencies.append((time.perf_counter() - started) * 1000)
        retrieved = [id_to_dsid.get(node, "") for node, _ in hits]

        expected = set(q["expected"])
        found = expected & set(retrieved)
        recall = len(found) / len(expected)
        # Rank of the first expected document, which is what decides whether a
        # bounded evidence budget ever sees it.
        rank = next((i + 1 for i, d in enumerate(retrieved) if d in expected), None)

        rows.append({**q, "retrieved": retrieved, "recall": recall,
                     "hit_any": bool(found), "first_rank": rank})
        per_type[q["question_type"]].append(recall)

    total = len(rows)
    any_hit = sum(1 for r in rows if r["hit_any"])
    full = sum(1 for r in rows if r["recall"] == 1.0)
    mean_recall = sum(r["recall"] for r in rows) / max(total, 1)
    ranked = [r["first_rank"] for r in rows if r["first_rank"]]

    print(f"overall over {total} questions")
    print(f"  document recall (mean)   {mean_recall:.3f}")
    print(f"  at least one expected    {any_hit}/{total} ({any_hit / total:.1%})")
    print(f"  every expected document  {full}/{total} ({full / total:.1%})")
    if ranked:
        ranked.sort()
        print(f"  median rank of first hit {ranked[len(ranked) // 2]}")
    latencies.sort()
    print(f"  retrieval latency p50 {latencies[len(latencies) // 2]:.1f}ms "
          f"p95 {latencies[int(len(latencies) * 0.95)]:.1f}ms")

    print("\nby question type")
    for qtype, values in sorted(per_type.items(), key=lambda kv: -len(kv[1])):
        mean = sum(values) / len(values)
        hit = sum(1 for v in values if v > 0) / len(values)
        print(f"  {qtype[:34]:36} n={len(values):>3}  recall={mean:.3f}  "
              f"any-hit={hit:.1%}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "k": args.k, "questions": total,
        "mean_recall": mean_recall, "any_hit": any_hit, "full_recall": full,
        "by_type": {k: sum(v) / len(v) for k, v in per_type.items()},
        "rows": rows,
    }, indent=2))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
