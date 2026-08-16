#!/usr/bin/env python
"""Cost and validate a claim-extraction batch on a small pilot before bulk spend.

PLAN.md puts a pilot in front of tier-1 extraction so the projection comes from
measured usage rather than a token guess. This submits a handful of real corpus
documents through the Message Batches API, validates every returned span against
its source, and projects the full run from what the pilot actually consumed.

    uv run python scripts/40_pilot_batch.py --docs 50
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config  # noqa: E402
from tracegraph.llm import (  # noqa: E402
    build_batch_requests,
    collect_batch_results,
    poll_batch,
    submit_batch,
)
from tracegraph.parsers import normalise_content  # noqa: E402

# Published list prices per million tokens for the pinned extraction model.
# Batch processing is half price, which is why bulk extraction goes through it.
PRICE_IN_PER_MTOK = 1.00
PRICE_OUT_PER_MTOK = 5.00
BATCH_DISCOUNT = 0.5


def sample_documents(count: int, sources: tuple[str, ...]) -> dict[str, str]:
    """Take documents from the high-value sources tier-1 extraction targets."""
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    out: dict[str, str] = {}
    per_source = Counter()
    quota = max(1, count // len(sources))

    for batch in parquet.iter_batches(
        batch_size=2000, columns=["doc_id", "source_type", "content"]
    ):
        data = batch.to_pydict()
        if data["source_type"][0] not in sources:
            continue
        for i in range(batch.num_rows):
            st = data["source_type"][i]
            if st not in sources or per_source[st] >= quota:
                continue
            body = normalise_content(data["content"][i])
            # Very short documents carry no claims and would flatter the average.
            if len(body) < 400:
                continue
            out[data["doc_id"][i]] = body[:8000]
            per_source[st] += 1
        if len(out) >= count:
            break
    print(f"  sampled {len(out)} documents {dict(per_source)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", type=int, default=50)
    ap.add_argument("--sources", default="confluence,fireflies,jira,github")
    ap.add_argument("--target", type=int, default=30000,
                    help="documents the full tier-1 run would cover")
    args = ap.parse_args()

    sources = tuple(s.strip() for s in args.sources.split(","))
    texts = sample_documents(args.docs, sources)
    if not texts:
        print("no documents sampled", file=sys.stderr)
        return 1

    requests = build_batch_requests(texts.items())
    print(f"  submitting {len(requests)} requests")
    batch = submit_batch(requests)
    print(f"  batch {batch.id}; polling until it ends")

    ended = poll_batch(batch.id)
    print(f"  {ended.processing_status}: {ended.request_counts}")

    result = collect_batch_results(batch.id, texts)
    manifest = result.manifest
    usage = manifest.total_usage

    print("\nextraction")
    print(f"  accepted claims : {manifest.accepted_claims}")
    print(f"  rejected claims : {manifest.rejected_claims}")
    print(f"  failed documents: {len(result.failures)}")
    print(f"  models returned : {manifest.returned_models}")

    if result.rejected:
        reasons = Counter(r.reason for r in result.rejected)
        print(f"  rejection reasons: {dict(reasons)}")

    in_tok = usage.input_tokens
    out_tok = usage.output_tokens
    docs = max(1, len(texts) - len(result.failures))

    cost = (in_tok / 1e6 * PRICE_IN_PER_MTOK
            + out_tok / 1e6 * PRICE_OUT_PER_MTOK) * BATCH_DISCOUNT
    per_doc = cost / docs

    print("\nmeasured usage")
    print(f"  input  {in_tok:>9,} tokens ({in_tok / docs:,.0f}/doc)")
    print(f"  output {out_tok:>9,} tokens ({out_tok / docs:,.0f}/doc)")
    print(f"  batch-priced cost ${cost:.4f} over {docs} documents "
          f"(${per_doc:.5f}/doc)")

    print("\nprojection from measured usage, not from an assumption")
    for n in (10_000, args.target, 100_000):
        print(f"  {n:>7,} documents  ${per_doc * n:>8.2f}")

    if manifest.accepted_claims == 0:
        print("\nno claims survived validation; do not proceed to bulk", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
