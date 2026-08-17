# Third-party notices

TraceGraph is released under the MIT licence (see `LICENSE`). It uses the
following third-party work, none of which is redistributed here.

## HydraDB — AGPL-3.0

<https://github.com/hydra-db/hydradb>

TraceGraph runs HydraDB as a **separate, unmodified containerised service** and
talks to it over its network interfaces — Bolt on 7687 and the HTTP query API on
8443. No HydraDB source is vendored, copied, linked, or modified in this
repository, and the pinned image is pulled from the project's own registry:

```
ghcr.io/hydra-db/hydradb@sha256:db78309a233be54662db29744047e985a39b51c45a270d1a1f47c31a62cdb709
```

The reference clone under `hydradb/` is read-only documentation, is gitignored,
and is re-materialised by `scripts/00_fetch_refs.sh`.

This is a statement of how the software is used, not legal advice. Anyone
redistributing this project should take their own view of their obligations.

## EnterpriseRAG-Bench — MIT

<https://github.com/onyx-dot-app/EnterpriseRAG-Bench> (Onyx)

The corpus and its 500 evaluation questions. Not redistributed: `dataset/` and
`external/` are gitignored, and the evaluation reads them from a local checkout.

The benchmark's answer key — `gold_answer`, `answer_facts`, `expected_doc_ids` —
is read only by evaluation code, never by ingestion or answering. The questions
file has exactly two readers, and neither can do the other's job:
`parquet_reader.read_questions` serves question text and refuses every gold
column outright, and `parquet_reader.read_answer_key` returns `question_id` and
`expected_doc_ids` and nothing else — no question text, so a caller holding the
key cannot also hold the thing being measured. `scripts/75_retrieval_eval.py` is
the only caller of the second, and it passes only `question` to the retriever.

The benchmark's generator also ships an identity oracle mapping every person to
their email, title, and manager. Resolving aliases against it would answer the
exact question this project exists to solve, so it is quarantined in the
gitignored `eval-oracle/`. Nothing in `tracegraph/` or `scripts/` reads it —
`scripts/00_fetch_refs.sh` only moves it into quarantine — so it is held for
scoring that has not yet been written rather than scoring already done. See
`docs/refs.lock.md`.

## Salesforce HERB — CC-BY-NC-4.0

<https://huggingface.co/datasets/Salesforce/HERB>

Present in the local dataset checkout but **not used** by any code in this
repository. Research and non-commercial use only; not redistributed.

## Anthropic Claude API

<https://www.anthropic.com>

Claim extraction uses `claude-haiku-4-5-20251001`; answer synthesis uses
`claude-sonnet-5`. The model returned by the API is recorded in every extraction
manifest alongside token usage, so a run's provenance and cost can be audited
after the fact.

## Python dependencies

`pyarrow`, `neo4j` (driver), `httpx`, `anthropic`, `fastapi`, `uvicorn`,
`python-dotenv`, `pytest` — each under its own licence, resolved and pinned in
`uv.lock`.
