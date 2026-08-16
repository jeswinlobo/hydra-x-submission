#!/usr/bin/env python
"""Ask a question against the ingested graph and print the answer contract.

    uv run python scripts/50_ask.py "who owns the perf-canary runbook?"
    uv run python scripts/50_ask.py --demo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config  # noqa: E402
from tracegraph.controller import AnswerController  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.parsers import normalise_content  # noqa: E402

DEMO_QUESTIONS = [
    "What is the rollback procedure for the perf-canary service?",
    "Which quantization profile caused the latency regression?",
    "What is the Q4 2029 revenue target for the Antarctic division?",
]


def load_bodies(client: HydraClient, run_id: str) -> dict[str, str]:
    """Bodies for the run's documents, so spans can be re-checked at answer time."""
    rows = client.bolt_read(
        "MATCH (d:Document) WHERE d.run_id = $r RETURN d.dsid AS dsid", {"r": run_id})
    wanted = {row["dsid"] for row in rows}
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    bodies: dict[str, str] = {}
    for batch in parquet.iter_batches(batch_size=4000, columns=["doc_id", "content"]):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            dsid = data["doc_id"][i]
            if dsid in wanted:
                bodies[dsid] = normalise_content(data["content"][i])
        if len(bodies) == len(wanted):
            break
    return bodies


def show(result, question: str) -> None:
    contract = result.to_contract()
    print(f"\n{'=' * 74}\nQ: {question}\n{'=' * 74}")
    print(f"\nanswerability: {contract['answerability']}  "
          f"confidence: {contract['confidence']}")
    print(f"\n{contract['answer']}\n")

    if contract["document_ids"]:
        print("citations (each validated against the graph):")
        for dsid in contract["document_ids"]:
            print(f"  {dsid}")

    if result.claims:
        print("\nsupporting claims, each with a span verified verbatim in its source:")
        for claim in result.claims[:4]:
            print(f"  {claim['subject']} — {claim['predicate']} — {claim['object']}")
            print(f"    \"{claim['quote'][:110]}\"")

    if result.rejected_citations:
        print(f"\nrejected citations: {result.rejected_citations}")
    if result.rejected_spans:
        print(f"rejected spans: {len(result.rejected_spans)} no longer verbatim")

    trace = contract["hydradb_trace"]
    print(f"\nhydradb trace: {trace['query_count']} queries, max {trace['hops']} hops, "
          f"{trace['latency_ms']}ms")
    print(f"  read_epoch={trace['consistency']['read_epoch']} "
          f"storage_sequence={trace['consistency']['storage_sequence']}")
    for q in trace["queries"][:3]:
        print(f"  {q['operation']:22} {q['results']:>4} rows  {q['latency_ms']:>6}ms")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question", nargs="*", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--demo", action="store_true", help="run the three demo questions")
    ap.add_argument("--json", action="store_true", help="print the raw contract")
    args = ap.parse_args()

    with HydraClient() as client:
        client.verify()
        run_id = args.run_id
        if not run_id:
            rows = client.bolt_read(
                "MATCH (d:Document) RETURN d.run_id AS run_id ORDER BY run_id DESC LIMIT 1")
            run_id = rows[0]["run_id"] if rows else None
        if not run_id:
            print("no ingested run found", file=sys.stderr)
            return 1

        bodies = load_bodies(client, run_id)
        controller = AnswerController(client, run_id)

        questions = DEMO_QUESTIONS if args.demo else [" ".join(args.question)]
        if not any(q.strip() for q in questions):
            print("give a question, or --demo", file=sys.stderr)
            return 1

        for question in questions:
            result = controller.answer(question, bodies=bodies)
            if args.json:
                print(json.dumps(result.to_contract(), indent=2)[:4000])
            else:
                show(result, question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
