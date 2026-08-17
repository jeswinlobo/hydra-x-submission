#!/usr/bin/env python
"""Run the demo questions repeatedly and refuse to pass on a flaky result.

PLAN.md requires ten consecutive clean runs before recording, because a demo
that fails once in ten will fail on camera.

What it checks is invariants, not exact labels. Language-model output is not
byte-identical between runs and is not required to be, and one verdict genuinely
moves with it: whether an answer is `conflicting` depends on whether the prose
the model wrote states a value another document disputes. Pinning that to a
single expected label failed three rounds in forty, all three of them correct
behaviour. So each question lists the verdicts that are acceptable, and what is
held fixed is what a viewer would actually notice going wrong:

* an abstention carries no citations and no claims;
* a `conflicting` verdict names the competing version rather than just asserting
  a dispute;
* a `supported` answer is not quietly sitting on a dispute the system found;
* every returned citation exists in the graph under a labelled match.

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
        # An empty graph is a starting state, matching 50_ask.py and the API:
        # documents are enriched when questions reach them, so `bootstrap.sh
        # --fast` can still be checked. It refused outright before, which made
        # the command bootstrap prints on that path exit 1 immediately.
        run_id = rows[0]["r"] if rows else "ondemand"
        if not rows:
            print("no preloaded run; answering on demand over the whole corpus\n",
                  file=sys.stderr)
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

                # `expected` is a set of acceptable verdicts, not one label.
                #
                # A single label was the wrong test and the check itself proved
                # it: whether an answer is `conflicting` depends on whether the
                # prose the model wrote states a value some other document
                # disputes, so the same question legitimately came back
                # `supported` in six rounds and `conflicting` in two — both
                # correct. What must never vary is the invariants below, and
                # those are what this now holds the system to.
                allowed = {expected} if isinstance(expected, str) else set(expected)
                ok = result.answerability in allowed
                if not ok:
                    failures.append(
                        f"round {round_no}: {question[:44]!r} returned "
                        f"{result.answerability}, expected one of {sorted(allowed)}")

                # An abstention that cites anything is not an abstention.
                if result.answerability == "insufficient" and (
                        result.document_ids or result.claims):
                    failures.append(
                        f"round {round_no}: abstention carried "
                        f"{len(result.document_ids)} citation(s) and "
                        f"{len(result.claims)} claim(s)")
                    ok = False

                # A conflicting verdict has to name what is in dispute, or it is
                # a label with nothing behind it.
                if result.answerability == "conflicting" and not result.alternatives:
                    failures.append(
                        f"round {round_no}: conflicting verdict carried no "
                        "competing version")
                    ok = False

                # Conversely, a supported answer must not be sitting on a
                # dispute the system found and then failed to report.
                if result.answerability == "supported" and result.alternatives:
                    failures.append(
                        f"round {round_no}: supported answer carried "
                        f"{len(result.alternatives)} contested fact(s)")
                    ok = False

                # Every citation has to survive a labelled existence check.
                for dsid in result.document_ids:
                    if not controller.citation_exists(dsid):
                        failures.append(
                            f"round {round_no}: cited {dsid} is not in the graph")
                        ok = False

                line.append(f"{'ok' if ok else 'FAIL'}:{result.answerability[:4]}"
                            f"/{len(result.document_ids)}c"
                            f"{'/!' + str(len(result.alternatives)) if result.alternatives else ''}"
                            f"/{elapsed:.0f}s")
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
