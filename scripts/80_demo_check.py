#!/usr/bin/env python
"""Run the demo questions repeatedly and refuse to pass on a flaky result.

PLAN.md requires ten consecutive clean runs before recording, because a demo
that fails once in ten will fail on camera. This checks more than "did it not
crash": each question has an expected answerability, every returned citation
must exist in the graph, and a question meant to abstain must return no
citations at all.

Language-model output is not byte-identical between runs and is not required to
be. What must hold is the part a viewer would notice — the verdict, the
citations, and whether anything was fabricated.

    uv run python scripts/80_demo_check.py --rounds 10
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph.controller import AnswerController  # noqa: E402
from tracegraph.demo import DEMO_QUESTIONS as DEMO  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ingest import OnDemandIngestor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=10)
    args = ap.parse_args()

    failures: list[str] = []
    latencies: list[float] = []

    with HydraClient() as client:
        client.verify()
        rows = client.bolt_read(
            "MATCH (d:Document) RETURN d.run_id AS r ORDER BY r DESC LIMIT 1")
        if not rows:
            print("no ingested run; run scripts/30_load_slice.py", file=sys.stderr)
            return 1
        run_id = rows[0]["r"]
        ingestor = OnDemandIngestor(client, run_id)

        for round_no in range(1, args.rounds + 1):
            line = [f"round {round_no:>2}"]
            for question, expected in DEMO:
                controller = AnswerController(client, run_id, ingestor=ingestor)
                started = time.perf_counter()
                try:
                    result = controller.answer(question, bodies={})
                except Exception as exc:  # noqa: BLE001 - a crash is the thing being tested
                    failures.append(f"round {round_no}: raised {type(exc).__name__}: {exc}")
                    line.append("CRASH")
                    continue
                elapsed = time.perf_counter() - started
                latencies.append(elapsed)

                ok = result.answerability == expected
                if not ok:
                    failures.append(
                        f"round {round_no}: {question[:44]!r} returned "
                        f"{result.answerability}, expected {expected}")

                # An abstention that cites anything is not an abstention.
                if expected == "insufficient" and result.document_ids:
                    failures.append(
                        f"round {round_no}: abstention cited {result.document_ids}")
                    ok = False

                # Every citation has to survive a labelled existence check.
                for dsid in result.document_ids:
                    if not controller.citation_exists(dsid):
                        failures.append(
                            f"round {round_no}: cited {dsid} is not in the graph")
                        ok = False

                line.append(f"{'ok' if ok else 'FAIL'}:{result.answerability[:4]}"
                            f"/{len(result.document_ids)}c/{elapsed:.0f}s")
            print("  ".join(line), flush=True)

        ingestor.close()

    print()
    if latencies:
        latencies.sort()
        print(f"latency  p50 {statistics.median(latencies):.1f}s  "
              f"p95 {latencies[int(len(latencies) * 0.95) - 1]:.1f}s  "
              f"max {latencies[-1]:.1f}s")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for failure in failures[:12]:
            print(f"  {failure}")
        print("\nNOT ready to record.")
        return 1

    print(f"\n{args.rounds} consecutive clean rounds. Ready to record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
