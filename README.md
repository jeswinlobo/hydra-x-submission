# TraceGraph

**An enterprise truth debugger on HydraDB.**

Half a million documents from nine business tools disagree with each other. The
same person appears as `sam`, `Sam Carter`, and `sam.carter@dataforge.ai` — and
`sam` alone has nineteen plausible referents. One employee, Marissa Cole, carries
ten different job titles depending on which document you read, and two different
people named Priya Sharma work at two different companies. Ask a question and a
search engine will hand you a document; it will not tell you whether the document
is right, who else contradicts it, or how it knows.

TraceGraph answers questions over the corpus and shows its work: which entities
and claims are connected, which sources support or contradict them, and the exact
evidence path behind every answer. When the evidence does not support an answer,
it says so instead of inventing one.

Built for [Hack Hydra](https://hackhydra.hydradb.com) Track 1 — Enterprise
Context & Ontology.

---

## What it does

Ask anything about the corpus. Retrieval searches all **511,962 rows** — 511,958
distinct documents, four doc_ids being exact duplicates that are deduplicated on
ingest — and whatever it reaches is parsed, resolved, and extracted into the
graph during the request, then stays there for later questions.

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

Ask something the corpus contradicts itself about and it says so, with both
versions and the document behind each:

> **Q: Interview slate and role anchors for the Staff Inference Engineer opening**
>
> **conflicting** · confidence 0.6 · 5 contested facts
>
> `Grace O'Connor — works as`
> cited *Hiring Manager, Inference Runtime* · rival *Director, Talent Strategy*
> · evidence does not decide between them

Ask something the corpus does not contain and it abstains — no citations, no
claims, no guess.

## Why HydraDB is doing real work

The graph is not a place results are filed after the fact. Four things are
decided by traversal, and none of them survives if HydraDB is removed.

**Entity resolution.** Slack is 55.8% of the corpus and its speakers are bare
first names. `sam` has nineteen plausible referents; `alex` has forty-eight.
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

On the loaded slice this decided **766 mention occurrences**, spanning **24
distinct ambiguous surfaces** — `alex` resolved to Alex Chen over 47 competitors,
at 0.95 confidence. The two numbers are far apart on purpose: bare first names
are few but they recur constantly, so a handful of genuinely ambiguous strings
accounts for most of the volume. `scripts/35_verify_gate.py` reports both,
straight out of the graph.

Where the graph does not separate the candidates the mention stays unresolved
with its candidate set recorded — **1,682 mentions across 72 surfaces** — because
a wrong merge is worse than an honest "cannot tell".

**Resolution is judged in both directions, so it refuses in both.** Splitting one
person into many is the obvious failure; fusing two people into one is the
quieter and worse one, because it produces a confident answer attributed to
somebody who never said it. A shared full name is therefore not sufficient to
merge — the identities must also share an organisational root:

| | |
|---|---|
| `grace@redwood.com` + `grace.oconnor@redwood.ai` + `grace@redwoodinference.com` | one Grace O'Connor, 14 addresses |
| `priya@mediloop.com` + `priya.sharma@procureco.com` | two Priya Sharmas |

Getting that wrong is not hypothetical. An earlier rule merged on name alone and
put 76 identities across unrelated companies, collecting 366 mentions between
them — Elena Rossi at cardiotech.com had absorbed Elena Rossi at microsoft.com,
and a `procurement@` role address had been folded into a person. Those 366 were
*attached to a conflated identity*, which is not the same as 366 individually
wrong answers: a mention of the cardiotech Elena still landed on an entity that
was partly her. It is the exposure, not a proven error rate, and the distinction
matters because overstating it would be the same failure the merge rule is
guarding against.
`scripts/37_rebuild_resolution.py` re-decides every identity in the graph and
**deletes** the edges the old rule produced, which the engine permits only
through a form documented in `docs/engine-notes.md`. The graph now holds **zero**
identities spanning unrelated organisations and **zero** mentions carrying two
resolutions; the script is idempotent, so a second run finds nothing to change.

**Provenance.** Claims and evidence spans are nodes, not properties, so a span
can be inspected on its own and one span can support several claims:

```
Document -[:ASSERTS]-> Claim -[:SUPPORTED_BY]-> EvidenceSpan
```

**Conflict resolution, in the answer itself.** The brief names four things a
question can need — a lookup, multi-hop reasoning, conflict resolution, and
knowing when the answer is absent. The fourth was the one this got wrong for a
while: `CONFLICTS_WITH` edges existed and a panel displayed them, but the answer
path never looked, so a question about a disputed fact came back confident and
singular.

It now walks one hop from the claims the answer used and returns
`answerability: "conflicting"` with both versions and where each came from:

> **Interview slate and role anchors for the Staff Inference Engineer opening**
> **conflicting** · confidence 0.6 · 5 contested facts
>
> `Grace O'Connor — works as`
> cited *Hiring Manager, Inference Runtime* · rival *Director, Talent Strategy*
> — evidence does not decide between them

Precision here cost two attempts, both caught by the stability check. Anchoring
on cited *documents* flagged a SOC 2 answer over a job title it never mentioned;
anchoring on the evidence claims still over-flagged, because every claim
extracted from a cited document is handed to the model, not only the ones it
used. The test that holds is whether the answer **states** the contested value.
Crying wolf costs exactly what silence costs.

**Detection runs where the claims are written**, not once at setup.
`CONFLICTS_WITH` edges used to be produced by a single bootstrap pass, so the
answer path — which walks persisted edges — could only see disputes that existed
then. A disagreement introduced by a document a question had just reached was
invisible, and under `--fast`, where nothing is preloaded, every disagreement
was: 23 edges for 2,606 claims. On-demand ingestion now re-adjudicates the facts
its documents touch, once per batch, and the graph holds 234 edges for 3,154
claims — fewer than the 375 an earlier, looser rule produced, because 31 of
those joined two different people and 181 more did not survive the corrected
grouping. `scripts/55_conflicts.py` remains for a full sweep; the two agree because
both call the same detector and edge ids are deterministic, so a pair judged
twice converges on one edge.

Three things had to be right for that to be correct rather than merely present,
and the first two were wrong until a live comparison against a full sweep found
73 edges missing and 31 that should never have existed.

**Selection and adjudication now share one definition of "the same fact",**
because having two was the bug twice over. Selection compared raw predicates
while adjudication compares aligned ones, so `has job title` never reached
`works as` — 73 missing edges, every one an alias of `holds_title`. Fixing that
in one place left the identical fault on the subject: `S. Ratnaparkhi` never
reached `Sam` even where the resolver had already decided they are one person.
Both callers now ask `conflicts.group_key`, so they cannot drift apart again.

**A fact is about a person, not a name.** Grouping on the subject string put
Anna Liu at cedarwave.com and Anna Liu at cloudwave.com into one dispute about
one person's employer, and did the same to two Elena Rossis at unrelated
companies. Adjudication now groups by the identity the resolver decided on,
falling back to the name only where there is no identity to use — most subjects
are not people. Incremental and sweep now agree exactly: **227 expected, 227
persisted, 0 joining different identities**, and the sweep also *removes* edges
it no longer produces, because a rule that changes has to retract what the old
one wrote.

**The read shape is the opposite of what looks obvious.** Fetching only the
competing versions of one fact walks the Document label to reach the claim and
cost **3.4 seconds each**, thirty-six per question — a cold question went from
30 seconds to 275. One bulk load of every claim costs 3.5 seconds. Likewise the
identity map: walking `RESOLVES_TO` to the entity cost 7.6 seconds against 0.7
for reading the mentions alone, so the resolved entity is denormalised onto the
mention, which already records *how* it resolved. A narrower query is not a
cheaper one when cost tracks the anchor label rather than the rows returned.

`algo.SPpaths` returns the path connecting a resolved identity to a channel it
participates in, and the panel renders the elements the engine returned rather
than a sentence composed about them — so the explanation and the graph cannot
drift apart. Be precise about its size: the engine hands back a flat alternating
list, so a single participation edge arrives as three elements and is **one
hop**, shown as one. The multi-hop reasoning is in the scoring that precedes it —
a two-hop co-occurrence walk and a one-hop participation check per candidate —
not in the rendered path. `algo.SSpaths` and `algo.MSpaths` are pinned by contract
tests (`tests/test_hydra_contract.py`) but are not on the answer path.

Every answer carries the engine's own `read_epoch` and bookmark, so the
consistency position that produced it is visible alongside it.

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

| intra-doc | conflicting | misc | constrained | basic | project | completeness | **semantic** |
|---|---|---|---|---|---|---|---|
| 0.925 | 0.900 | 0.900 | 0.883 | 0.817 | 0.768 | 0.584 | **0.488** |

All eight types the benchmark carries, none omitted. The recorded run is
committed at `artifacts/retrieval_summary.json`, so these are checkable without
re-running anything.

That 0.488 is the number graph reasoning has to move, and it is why the graph
exists rather than a bigger index.

**Evidence discipline, measured rather than asserted.** Of 505 claims the model
produced in a pilot batch, **62 (12%) cited evidence that does not appear
verbatim in the source and were rejected**. A span altering a single word of a
real sentence is refused (`tests/test_conflicts.py`, `tests/test_parsers.py`).

**Ingestion**: ~19,000 nodes/second measured; the whole corpus normalises in 22
seconds and indexes in 312. Registering 511,958 document ids produced **zero
collisions**.

**Demo stability.** Ten consecutive rounds of the four demo questions — one
supported, one contested, one either, one unanswerable — all clean
(`scripts/80_demo_check.py`). It checks invariants rather than exact prose:
every citation exists in the graph under a labelled match, an abstention carries
no citations or claims, a `conflicting` verdict names the rival version, and a
`supported` answer is not sitting on a dispute the system found. It also refuses
to pass a run in which no question came back contested, because a controller
that had quietly stopped detecting conflicts would otherwise score ten out of
ten. Latency p50 7.7s, p95 13.1s.

That p50 was 29.6s until the corpus was re-chunked. The file ships as a single
row group holding all 511,962 documents, and parquet decodes a row group whole,
so fetching one document read the entire 1.4 GB file — 4.5 seconds, paid four
times per question because on-demand ingestion enriches several candidates at
once. Answering spent longer scanning parquet than talking to the model.
`scripts/71_repartition_corpus.py` writes a lossless copy at 2,048 rows per group
and indexes that: **4,465ms → 12ms per document fetch**, and the preload scan on
the first question disappears entirely.

**Graph expansion of retrieval was tried, measured, and removed.** The obvious
next move is to widen retrieval by traversal: take the documents search found,
walk `Document ←MENTIONED_IN— Mention —RESOLVES_TO→ Entity ←RESOLVES_TO— Mention
—MENTIONED_IN→ Document`, and read the neighbours. It works as a query — five
hops, ~250 ms, and it does reach documents lexical search did not.

It changed no answers. On every question tried it produced the same citations
and the same claims one to three seconds slower, and gating it to fire only on
thin evidence made it fire never: search returns eight documents, each carrying
around twenty extracted claims, so evidence is never thin. The bottleneck is not
how much evidence there is but whether the right document was retrieved, and
expanding from the wrong seed reaches the wrong neighbours. It was removed
rather than shipped as an impressive-sounding path nothing takes.

Where the graph *does* do multi-hop work is identity: 766 mention occurrences
resolved by traversal over stored structure, scored by a two-hop co-occurrence
walk and a one-hop participation check. `algo.SPpaths` returns the path behind a
participation decision, and the panel renders the path the engine returned rather
than a summary of it — currently a single `PARTICIPATED_IN` edge, reported as the
one hop it is. That is a real traversal answer to a question an index cannot
answer; retrieval expansion was not.

**No graph-vs-no-graph ablation was run.** PLAN.md called for four variants —
lexical only, hybrid, hybrid plus graph structure, full TraceGraph — and only
the first was measured; the retrieval numbers above *are* the lexical baseline.
So the case for the graph rests on the capability argument above and on the
resolution decisions the gate reads back, not on a measured answer-quality
delta. That is the honest state of it.

**Honest limits.** A cold question — one reaching documents the graph has not
seen — takes 25–40 seconds, because enriching them during the request means live
model calls; repeat questions are fast.
Answer-quality scores against the official evaluator are **not** reported — that
harness was not run, and the retrieval numbers above are the measurements this
project actually made.

## Quickstart

Requires Docker, Python 3.10+ (developed on 3.12), [uv](https://docs.astral.sh/uv/),
and an Anthropic API key. Budget **~7 GB free disk** (1.4 GB corpus, a 1.4 GB
re-chunked copy, a 2.5 GB lexical index) and **~8 GB RAM** — the container is
capped at 6 GB in `docker-compose.yml`, so lower that first on a smaller machine.

```bash
git clone https://github.com/jeswinlobo/hydra-x && cd hydra-x
cp .env.example .env          # add ANTHROPIC_API_KEY
# corpus: https://huggingface.co/datasets/onyx-dot-app/EnterpriseRAG-Bench
#   -> dataset/EnterpriseRAG-Bench/data/{documents,questions}/test.parquet

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
uv run pytest                             # 177 tests, 27 against the live engine
uv run python scripts/35_verify_gate.py   # 11 checks, read back from the graph
uv run python scripts/36_repair_graph.py  # audit identities and undecided mentions
uv run python scripts/37_rebuild_resolution.py  # re-decide every identity, report drift
uv run python scripts/75_retrieval_eval.py --limit 470
```

Every command above is read-only. `scripts/55_conflicts.py` is a pipeline step,
not a check — it writes `CONFLICTS_WITH` edges — so it lives in `bootstrap.sh`
rather than here; `36_repair_graph.py` likewise only writes with `--apply`.

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
