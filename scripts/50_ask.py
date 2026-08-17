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

from tracegraph.controller import AnswerController  # noqa: E402
from tracegraph.demo import QUESTIONS as DEMO_QUESTIONS  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ingest import OnDemandIngestor  # noqa: E402


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

    # Shown apart from supporting claims, and only ever on an abstention. These
    # are what the system read and declined to rely on; printing them under the
    # same heading as support would make a refusal read like an answer.
    if result.examined:
        docs = sorted({c["dsid"] for c in result.examined})
        print(f"\nexamined and not relied on ({len(docs)} document(s) reached, "
              "none supporting an answer):")
        for claim in result.examined[:3]:
            print(f"  {claim['subject']} — {claim['predicate']} — {claim['object']}")

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
    ap.add_argument("--demo", action="store_true", help="run the four demo questions")
    ap.add_argument("--json", action="store_true", help="print the raw contract")
    args = ap.parse_args()

    with HydraClient() as client:
        client.verify()
        run_id = args.run_id
        if not run_id:
            rows = client.bolt_read(
                "MATCH (d:Document) RETURN d.run_id AS run_id ORDER BY run_id DESC LIMIT 1")
            run_id = rows[0]["run_id"] if rows else None
        # An empty graph is a starting state: documents are enriched when
        # questions reach them.
        run_id = run_id or "ondemand"

        # Bodies are fetched per document through the ingestor's row locator as
        # the controller needs them, so this starts empty and fills with the
        # handful of documents each question actually reaches.
        bodies: dict[str, str] = {}
        ingestor = OnDemandIngestor(client, run_id)
        controller = AnswerController(client, run_id, ingestor=ingestor)

        questions = list(DEMO_QUESTIONS) if args.demo else [" ".join(args.question)]
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
