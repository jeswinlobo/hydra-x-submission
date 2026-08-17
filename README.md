# TraceGraph

**An enterprise truth debugger on HydraDB.**

Half a million documents from nine business tools disagree with each other. The
same person appears as `sam`, `Sam Carter`, and `sam.carter@dataforge.ai`. One
employee holds seventeen different job titles depending on which email you read.
Ask a question and a search engine will hand you a document; it will not tell you
whether the document is right, who else contradicts it, or how it knows.

TraceGraph answers questions over the corpus and shows its work: which entities
and claims are connected, which sources support or contradict them, and the exact
evidence path behind every answer. When the evidence does not support an answer,
it says so instead of inventing one.

Built for [Hack Hydra](https://hackhydra.hydradb.com) Track 1 — Enterprise
Context & Ontology.

---

## What it does

Ask anything about the corpus. Retrieval searches all **511,962 documents**;
whatever it reaches is parsed, resolved, and extracted into the graph during the
request and stays there for later questions.

```
$ ./scripts/60_serve.sh          # → http://127.0.0.1:8000
```

> **Q: Which quantization profile caused the P95 latency regression?**
>
> **supported** · confidence 0.9 · 2 citations
>
> The passages describe two different incidents. In one, the overnight rollout of
> the `auto-calib-4bit` quantization profile caused the runtime to select an
> INT8-emulation kernel…
>
> `dsid_46243814fad04d06807c6f4d8546789f` · `dsid_bbaf064b5fbf45e4a639c2e51830fe5c`

Neither of those documents was preloaded. Retrieval found them among 511,962,
and they were enriched while the question was being answered.

Ask something the corpus does not contain and it abstains — no citations, no
guess.

## Why HydraDB is doing real work

The graph is not a place results are filed after the fact. Three things are
decided by traversal, and none of them survives if HydraDB is removed.

**Entity resolution.** Slack is 55.8% of the corpus and its speakers are bare
first names. `sam` has sixteen plausible referents; `alex` has forty-three.
String similarity cannot separate them. So participation is written into the
graph first — who speaks in which channel, which mention sits in which document
— and candidates are then scored by traversals over that structure:

```cypher
-- co-occurrence: is this candidate already in this very document?
MATCH (e:Entity {id: $eid})<-[:RESOLVES_TO]-(m:Mention)-[:MENTIONED_IN]->(d:Document {id: $did})
RETURN count(*) AS n

-- participation: does this candidate speak in this channel?
MATCH (e:Entity {id: $eid})-[:PARTICIPATED_IN]->(c:Channel {id: $cid})
RETURN count(*) AS n
```

On the loaded slice this decided **589 surfaces** that string matching cannot,
across **4,869 queries** to the engine — `alex` resolved to Alex Chen over 42
competitors. Where the graph does not separate the candidates, the mention stays
unresolved with its candidate set recorded, because a wrong merge is worse than
an honest "cannot tell".

**Provenance.** Claims and evidence spans are nodes, not properties, so a span
can be inspected on its own and one span can support several claims:

```
Document -[:ASSERTS]-> Claim -[:SUPPORTED_BY]-> EvidenceSpan
```

**Conflict topology.** Contested facts are `CONFLICTS_WITH` edges between claims,
so the answer path finds them by traversal rather than by recomputation.

Native path procedures (`algo.SPpaths`, `algo.SSpaths`, `algo.MSpaths`) return
whole evidence paths, which is what the interface renders. Every answer carries
the engine's own `read_epoch` and bookmark, so the consistency position that
produced it is visible alongside it.

**Without HydraDB** this degrades to a document retriever with no explainable
identity decisions, no traversable claim lineage, and no conflict graph.

## How it works

```
EnterpriseRAG-Bench Parquet  ──►  contentless FTS5 over all 511,962 documents
   (authoritative text)                     │
                                            ▼
                                   candidate documents
                                            │
                    ┌───────────────────────┴─────────────────────┐
                    ▼                                             ▼
          on-demand enrichment                          already in the graph
     parse → resolve → extract claims                            │
                    └───────────────────────┬─────────────────────┘
                                            ▼
                                         HydraDB
              entities · mentions · claims · evidence spans · conflicts
                                            │
                                            ▼
                            deterministic answer controller
                 evidence-bounded synthesis → citation validation → abstention
                                            │
                                            ▼
                                FastAPI + Ask & Inspect
```

**The division of labour is deliberate.** Parquet stays the authoritative text
store, lexical search covers corpus scale, and the graph holds the *enriched
working set* — documents that have been asked about. This is not only tidier; a
HydraDB label index holds at most 250,000 vertices, so putting all 511,962
documents under one label does not work (see `docs/engine-notes.md`).

The model writes prose. It does not decide what is true, what may be cited, or
whether a question is answerable — those are the controller's, and each is
settled by a check that can fail.

## Results

Retrieval, measured over all 511,962 documents against the 470 benchmark
questions that carry an answer key (`scripts/75_retrieval_eval.py`):

| Metric | Value |
|---|---|
| Mean document recall (top-20) | 0.736 |
| At least one expected document | 364/470 (77.4%) |
| Every expected document | 325/470 (69.1%) |
| Median rank of first correct document | 1 |

By question type, which is the useful cut — lexical search does well where
question wording overlaps the source and collapses where it does not:

| intra-doc | conflicting | constrained | basic | project | completeness | **semantic** |
|---|---|---|---|---|---|---|
| 0.925 | 0.900 | 0.883 | 0.817 | 0.768 | 0.584 | **0.488** |

That 0.488 is the number graph reasoning has to move, and it is why the graph
exists rather than a bigger index.

**Evidence discipline, measured rather than asserted.** Of 505 claims the model
produced in a pilot batch, **62 (12%) cited evidence that does not appear
verbatim in the source and were rejected**. A span altering a single word of a
real sentence is refused (`tests/test_conflicts.py`, `tests/test_parsers.py`).

**Ingestion**: ~20,000 nodes/second measured; the whole corpus normalises in 22
seconds and indexes in 312. Registering 511,958 document ids produced **zero
collisions**.

**Demo stability.** Ten consecutive rounds of the three demo questions, all
clean: verdicts as expected, every citation validated against the graph, and the
abstention citing nothing (`scripts/80_demo_check.py`). Latency p50 29.6s, p95
45.6s.

**Honest limits.** A cold question takes 25–40 seconds, because enriching
documents during the request means live model calls; repeat questions are fast.
Answer-quality scores against the official evaluator are **not** reported — that
harness was not run, and the retrieval numbers above are the measurements this
project actually made.

## Quickstart

Requires Docker, Python 3.10+, [uv](https://docs.astral.sh/uv/), and an
Anthropic API key.

```bash
git clone https://github.com/jeswinlobo/hydra-x && cd hydra-x
cp .env.example .env          # add ANTHROPIC_API_KEY
# place the corpus at dataset/EnterpriseRAG-Bench/data/

./scripts/bootstrap.sh        # everything, in order, ~10 minutes
./scripts/60_serve.sh         # → http://127.0.0.1:8000
```

`bootstrap.sh` checks its prerequisites before running anything slow, and every
stage is resumable, so it is safe to run twice. `--fast` skips the slice: questions
still work, because documents are enriched on demand, but the resolution and
conflict panels start empty.

`scripts/01_hydra_up.sh` does not report success on a listening port — it
round-trips a real query first, because a port is not proof.

## Verifying the claims above

```bash
uv run pytest                             # 103 tests, 18 against the live engine
uv run python scripts/35_verify_gate.py   # 11 checks, read back from the graph
uv run python scripts/55_conflicts.py     # contested facts + trust breakdown
uv run python scripts/75_retrieval_eval.py --limit 470
```

The gate script queries the graph rather than trusting the ingest, and its
checks include the one that matters most: that citation validation distinguishes
a real document id from one that was never written.

## What the engine taught us

`docs/engine-notes.md` records HydraDB behaviour verified against the pinned
image, with the probes that produced it. Several would have been silent bugs:

- **A bare `{id: N}` pattern is an address lookup, not an existence check.** It
  returns a row for ids that were never written. Citation validation written
  that way is vacuously true — the single most consequential finding here.
- Ingestion only works over Bolt; the HTTP query engine cannot execute a vertex
  upsert at all, though it is the transport that returns `read_epoch`.
- There is no batch multi-id read: `IN`, `UNWIND`-driven lookups, and
  multi-statement requests are all rejected.
- `UNWIND` batches cap at 1024 rows; a label index caps at 250,000 vertices, and
  overflowing it makes the label unscannable *including by the delete that would
  undo it*.

`tests/test_hydra_contract.py` pins each one, so an engine upgrade that breaks an
assumption fails loudly instead of corrupting the graph quietly.

## Documentation

| | |
|---|---|
| `PLAN.md` | The execution plan this was built against |
| `docs/engine-notes.md` | Verified HydraDB behaviour and its constraints |
| `docs/corpus-notes.md` | What the corpus actually contains |
| `docs/source-notes.md` | Document shapes for all nine sources |
| `docs/refs.lock.md` | Pinned upstream commits and firewalled material |

## Licence and attribution

MIT (`LICENSE`). HydraDB is AGPL-3.0 and is used as an unmodified, separately
containerised service over its network APIs — no source is vendored or modified.
Full third-party notices in `NOTICE.md`.

Built by [Jeswin Lobo](https://github.com/jeswinlobo) for Hack Hydra 2026.
