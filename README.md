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
versions and the document behind each. Every screenshot below is a real run
against the live system, captured by loading `?q=` on the running server — no
mock-ups:

![A conflicting answer: the verdict badge, two validated citations, supporting
claims each with a verbatim span, and three contested facts showing the cited
value against its rival with the document behind each](docs/images/conflict.png)

The contested panel is not decoration — the verdict came from walking
`CONFLICTS_WITH` one hop out from the claims the answer actually used, so an
answer resting on a disputed fact cannot come back singular and confident.

Ask something the corpus does not contain and it abstains — no citations, no
claims, no guess.

![An abstention: no citations, no claims, and an explicit statement that the
evidence does not answer the question](docs/images/abstention.png)

And the resolution panel shows both halves of the identity decision — the
surfaces folded into one person, the method and confidence behind each, the
`algo.SPpaths` path the engine returned for a participation decision, and the
surfaces deliberately left unresolved because the graph could not separate their
candidates:

![The entity resolution panel: several surfaces resolved to one entity with the
method and confidence for each, a rendered algo.SPpaths participation path, and
a "left ambiguous on purpose" table showing surfaces kept unresolved with their
candidate counts](docs/images/resolution.png)

## Why HydraDB is doing real work

The graph is not a place results are filed after the fact. Four things are
decided by traversal. Be precise about how much each one carries, because they
differ, and the differences are measurable.

**Entity resolution — the fourth tier, and what it actually decides.** Slack is
55.8% of the corpus and its speakers are bare first names. `sam` has nineteen
plausible referents; `alex` has forty-eight. String similarity cannot separate
them.

Resolution runs cheapest-first, and the graph is the last tier, not the first.
Of 6,270 `RESOLVES_TO` edges in the snapshot below: 4,632 (73.9%) are
`strong_key_email` — an address matched exactly, no graph involved — 549 are a
unique token subset, 15 an exact token set, and **1,074 (17.1%) are decided by
the graph**. The graph is handed the residue the string tiers could not touch,
which is the right order to try them in and also means most resolutions are not
graph decisions. Participation is written into the graph first — who speaks in
which channel, which mention sits in which document — and the remaining
candidates are scored by traversals over that structure:

```cypher
-- co-occurrence: is this candidate already in this very document?
MATCH (e:Entity {id: $eid})<-[:RESOLVES_TO]-(m:Mention)-[:MENTIONED_IN]->(d:Document {id: $did})
RETURN count(*) AS n

-- participation: does this candidate speak in this channel?
MATCH (e:Entity {id: $eid})-[:PARTICIPATED_IN]->(c:Channel {id: $cid})
RETURN count(*) AS n
```

At the snapshot in `artifacts/graph_snapshot.json` — read epoch
21628, 1421 documents — this decided
**1,074 mention occurrences** spanning
**38 distinct ambiguous surfaces**. The two
numbers are far apart on purpose: bare first names are few but they recur
constantly, so a handful of genuinely ambiguous strings accounts for most of the
volume.

Where the graph does not separate the candidates the mention stays unresolved
with its candidate set recorded — **2,000
mentions across 78 surfaces** — because a
wrong merge is worse than an honest "cannot tell".

**What those 1,074 decisions look like when you read them, which is less than
the query shape suggests.** Every `graph_evidence` edge stores its own
justification, and 1,071 of the 1,074 end the same way: *"against no graph
evidence for the next of N candidates."* The runner-up scored zero in all but three of
them, which is why almost all carry the identical confidence 0.95. So the traversal
is not adjudicating between two contenders with competing support — it is
finding the single candidate with any presence here at all, and recording that
the others had none.

That is worth stating plainly because the co-occurrence walk is also anchored on
one document (`(d:Document {id: $did})`), so its question is "has some other
mention *in this very document* already resolved to this person?" The entity it
lands on is global — established by every document ingested before this one, and
durable across restarts through `MERGED_INTO` — but the evidence weighed is
local, and a dictionary over the document being ingested would reproduce that
half. The participation check is the genuinely cross-document half, and it
contributes to the winner's score in most of these decisions; it has not yet
had to break a tie, because there have been no ties.

The honest summary is that this tier reads state the graph accumulated and
writes down why it chose, on the 17.1% of mentions no string rule could resolve.
It is not the two-way discrimination the query shape implies, and the earlier
draft of this section said it was.

**The graph proposes an identity, not just a ranking — and this is the case the
brief leads with.** Track 01 opens on one example: *"deciding that `Sam`,
`@soham` and `S. Ratnaparkhi` are one person."* Two of those three resolve by
token overlap. The first cannot, and the reason is worth being precise about:
`{sam}` is not a subset of `{soham, ratnaparkhi}`. There is no shared token, no
small edit distance, and nothing an embedding of two four-letter strings
recovers, because **the relationship is not in the text at all.**

It is in the graph. Somebody called `sam` speaks in a channel; Soham
Ratnaparkhi participates in that channel and is already resolved elsewhere in
this document. So when no string rule offers a candidate, the candidate set
comes from HydraDB — every person resolved inside this document or
participating in its channel — and the same co-occurrence and participation
traversals decide between them:

```cypher
MATCH (e:Entity)<-[:RESOLVES_TO]-(m:Mention)-[:MENTIONED_IN]->(d:Document {id: $did})
WHERE e.run_id = $r AND e.name STARTS WITH $initial
RETURN DISTINCT e.id, e.key, e.name
```

This is the tier that answers *"a use case that is hard to pull off with
traditional vector or relational approaches"* literally rather than
rhetorically. A vector index cannot reach `sam → Soham`; a SQL join cannot
either. Only the structure can.

It is also the weakest positive claim the resolver makes, and a wrong answer
here is a false merge — the failure the whole module exists to refuse. Three
guards, and `tests/test_graph_proposed.py` is nineteen cases of which most
assert a refusal:

* a single token of at least three characters, so it fires on short forms
  rather than on prose;
* a shared first initial. Every real short form has one, and without it the
  tier degrades to "one person is nearby, so the handle must be them";
* **a sole scoring candidate.** Two candidates with evidence means the graph
  cannot separate them, and the mention stays unresolved with that recorded.

Both halves are observable on the live graph. On one document the initial `S`
proposes Sean McCoy alone and resolves; on another, `M` proposes both Marcus
Lin and Markus Klein and the tier abstains. 27–467 ms. Confidence is capped at
0.70, below `graph_evidence`'s 0.80, because this tier has no lexical
corroboration beyond the initial and its ceiling should say so.

Why this existed as a gap until late: resolution returned `UNRESOLVED` the
moment `candidates_for()` came back empty, which is *before* the graph tier
ran. So the graph could only ever re-rank candidates a string rule had already
vouched for. That is also why every one of the 1,074 `graph_evidence` decisions
had a runner-up scoring zero — the tier was never handed a hard case, because
the hard cases were filtered out above it.

Every count here is generated, not typed. The graph grows whenever anyone asks a
question, so figures written into prose drift within hours; `scripts/35_verify_gate.py`
rewrites the snapshot each run and this section quotes it. If a number here
disagrees with the artifact, the artifact is right and the gate has not been
re-run.

**Scored against ground truth.** `eval-oracle/employee_directory.yaml` is the
benchmark generator's identity oracle — 167 people with their addresses. It is
quarantined from every resolution and answering path and used only here, by
`scripts/77_identity_eval.py` (deterministic, read-only, no model calls):

| | precision | recall | F1 |
|---|---|---|---|
| B³ | 100.0% | 89.9% | 94.7% |
| Pairwise | 100.0% | 74.9% | 85.7% |

**Read this table as a score for address normalisation, not for the graph.** Of
the 3,007 strictly-labelled mentions behind it, almost all were decided by
`strong_key_email` and 3 by an exact token set — the graph-evidence tier
contributes **zero** mentions to this number. What it does measure honestly is
fragmentation: whether `redwood.com`, `redwood.ai` and `redwoodinference.com`
collapse to one person. That is real and it is string processing. The graph
tier's own score is four paragraphs down and rests on twelve decidable
decisions.

**Zero false merges** — no entity fuses two directory people — against 3,007
strictly-labelled mentions, though note what that can and cannot show: every
directory full name is unique, so a false merge between two employees cannot
appear in this label set at all. The stronger evidence is gold-free — a sweep of
all 1,271 entities finds one that disagrees with itself — Grace O'Connor,
named below.

Splitting one person across several entities was the whole of the recall loss
and is largely fixed. Two numbers, because they count different things and
quoting only the flattering one would be the error this section is about: among
mentions carrying a strict label, fragments fell from 31 of 53 covered employees
to **1**, which moved B³ recall from 82.7%. Counting *entities* rather than
scored mentions, **33 of 106 covered employees still map to more than one
vertex** — mostly address spellings that appear in no labelled mention, so they
cost nothing in the table above and are still splits.

Read the recall, not the precision. The script says so itself, at length: 2,764
of 3,007 strict labels come from the same address tier 1 keys on, and every
directory full name is unique, so a false merge between two employees *cannot*
appear in that label set. And the graph-evidence tier — the one that justifies
HydraDB — is effectively unscorable here: 1,063 of its 1,074 decisions are on
one-token surfaces and 1,062 land on people the directory does not contain. Its
12 decidable decisions were all correct, which is a real result on a sample too
small to lead with. The oracle covers 106 of 1,271 entities; the rest are
customers and vendors it was never going to see.

**Resolution is judged in both directions, so it refuses in both.** Splitting one
person into many is the obvious failure; fusing two people into one is the
quieter and worse one, because it produces a confident answer attributed to
somebody who never said it. A shared full name is therefore not sufficient to
merge — the identities must also share an organisational root:

| | |
|---|---|
| `grace@redwood.com` + `grace.oconnor@redwood.ai` + `grace@redwoodinference.com` | Grace O'Connor, 14 addresses down to 2 entities |
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
**A merge is recorded, not just performed.** The folded vertex stays — deleting
it would strand whatever references it — so the decision is written as
`Entity -[:MERGED_INTO]-> Entity`. Without that, canonicalisation lasted exactly
as long as the process: the next resolver adopted the folded vertex again as its
own protected identity, two protected identities are never merged, and
`Camila Reyes` was back to six candidates on restart while mention-level splits
read as fixed. A fresh resolver now adopts 1,216 identities rather than 1,271,
and Camila, Naomi, Tessa and Grace each resolve to one *per organisation*. Be
exact about what that does and does not mean: counting entities rather than
scored mentions, `artifacts/identity_eval.json` still records Camila across six
address spellings, Naomi five, Tessa four and Grace two. Those extra vertices
carry no labelled mention, so they cost nothing in the table above and they are
still splits. Grace is the one identity the gold-free sweep flags.

The gate holds both halves: no mention carries two resolutions, and no full name
is split across live vertices *at the same organisation*. Ninety-eight names are
split across different organisations, which is two people rather than one
fragmented — merging those would be the false merge the whole module refuses.

`scripts/37_rebuild_resolution.py` re-decides every identity in the graph and
**deletes** the edges the old rule produced, which the engine permits only
through a form documented in `docs/engine-notes.md`. The graph now holds **zero**
identities spanning unrelated organisations and **zero** mentions carrying two
resolutions; the script is idempotent, so a second run finds nothing to change.

**Multi-hop, across documents and across sources.** Every parser has always
extracted ticket keys into `ParsedDoc.references`. Nothing read them. They are
the one exact, inference-free link between documents this corpus offers, so they
are now `Ticket` nodes, and the answer path walks four hops through them:

```cypher
MATCH (d:Document {dsid: $dsid})-[:REFERENCES]->(t:Ticket)
      <-[:REFERENCES]-(o:Document)-[:ASSERTS]->(c:Claim)
WHERE o.dsid <> $dsid
RETURN o.dsid, o.source_type, t.key, c.subject, c.predicate, c.object
```

This is the traversal lexical search cannot perform. A Slack thread naming
`PR-19855` and a Google Drive document naming `PR-19855` share almost no
vocabulary — one says "the retry storm", the other "per model/region gating" —
so no amount of term overlap connects them. The ticket key does, exactly.
Measured live: **24–49ms**, Slack reaching Google Drive.

It matters most for the six sources with no dedicated parser. Those fall through
to `generic`, which extracts no mentions at all, so a ticket key is the only
structure recoverable from them. Over the 1,421 ingested documents: **627
tickets, 662 `REFERENCES` edges, 31 tickets appearing in two or more documents,
joining 57 documents** — and documents carrying a ticket span all nine sources
(gmail 243, slack 114, linear 25, google_drive 19, jira 11, github 10,
confluence 9, hubspot 3, fireflies 1).

Be precise about what that is worth: 31 shared tickets is a small number, and
the reason is the graph holds 1,421 of 511,962 documents. Two documents citing
the same ticket are both present only rarely at that ratio, and the yield grows
roughly quadratically with the ingested slice. The traversal is correct and
cheap; its coverage is a function of how much has been ingested, not of the
query. `scripts/79_backfill_tickets.py` writes them for documents ingested
before this existed, and is idempotent.

The filter is where the risk sits. `[A-Z]{2,10}-\d{1,6}` is the shape of a
ticket and equally the shape of `AES-256`, `SOC-2` and `INC-2026`; a false
ticket joins two unrelated documents and the traversal would present that as a
connection. `tests/test_ticket_graph.py` holds 35 cases, most of them about what
must *not* become a ticket.

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
was: 23 edges for 2,606 claims. On-demand ingestion now re-adjudicates the facts its
documents touch, once per batch.

Counts here are a snapshot of a graph that grows as questions are asked, so they
are stated with the epoch they were taken at rather than as standing facts. At
**read_epoch 13015, 176 claim-bearing documents, 3,330 claims**: 115 contested
facts, 775 persisted `CONFLICTS_WITH` edges, 2 decided. The gate asserts the
detector and the graph agree exactly — 775 detected, 775 persisted, 0 missing —
because a panel that recomputes disputes while answers walk stored edges can
otherwise show a conflict the answer cannot see. Thirty-one edges were missing
when that check was first written. `scripts/55_conflicts.py` remains for a full sweep; the two agree because
both call the same detector and edge ids are deterministic, so a pair judged
twice converges on one edge.

**What conflict detection can reach, stated because the ceiling is low and the
reason is deliberate.** `ontology.py` models eleven canonical predicates, five
of them single-cardinality — and only a single-cardinality predicate can
conflict, because two values for a multi-valued relation are not a
disagreement. Extraction, meanwhile, produces whatever the documents say: at the
snapshot below, **3,187 distinct raw predicates across 6,553 claims**. Of those,
2,142 claims (32.7%) align to a canonical predicate and **1,572 (24.0%) are on a
predicate that can conflict at all**. The remaining 4,411 stay raw.

So conflict detection sees roughly a quarter of the graph's claims. That is a
catalogue that declines rather than guesses — the alternative is forcing
`has duration` and `supports` into a category and manufacturing disputes between
facts that do not compete — and the long tail is mostly junk (`is` accounts for
301 claims on its own). But a quarter is the honest reach, and a wider ontology
is the clearest single thing that would extend it.

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
not in the rendered path. `algo.MSpaths` is pinned by a contract test; `algo.SSpaths` is not, and
neither is on the answer path.

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

**Where the model's judgement ends.** The model writes prose and it reports
whether the passages it was given are sufficient. It does not decide what may be
cited, whether a span is real, or whether the answer sits on a dispute — those
are the controller's, and each is settled by a check that can fail: a citation
outside the supplied set is dropped, a citation that does not resolve to a
document under a labelled match is dropped, a span no longer verbatim in its
source is dropped, and an answer stating a contested value comes back
`conflicting` whatever the model called it.

Abstention is the one verdict the two share. The controller abstains outright
when the graph yields no evidence and when no returned citation survives
validation; between those, `sufficient` is the model's call. Saying otherwise
would be tidier and would not be true — which is why abstention is measured
against the benchmark's own unanswerable questions rather than asserted.

## Results

Retrieval, measured over all 511,962 documents against the 470 benchmark
questions that carry an answer key (`scripts/76_recall_by_budget.py`).

**Reported at the budget production actually uses.** Retrieval keeps eight
documents per question, so top-20 recall would flatter the system by describing
a configuration it does not run:

| Budget | Mean recall | Semantic recall | |
|---|---|---|---|
| 4 — cold enrichment | 0.587 | 0.312 | documents a cold question enriches |
| **8 — production** | **0.671** | **0.424** | **what the answer path sees** |
| 20 | 0.736 | 0.488 | wider than anything downstream consumes |

At the production budget of eight: at least one expected document for **73.0%**
of questions (343/470), every expected document for **61.5%** (289/470), median
rank of the first correct document **1**. Widening to twenty would make those
77.4% and 69.1%, which is stated here only so the two are not mistaken for each
other; nothing downstream sees a twentieth document.

By question type, which is the useful cut — lexical search does well where
question wording overlaps the source and collapses where it does not. The
production row is the one to read; top-20 is beneath it for comparison only:

| Budget | misc | intra-doc | constrained | conflicting | basic | project | completeness | **semantic** |
|---|---|---|---|---|---|---|---|---|
| **8 — production** | **0.900** | **0.875** | **0.833** | **0.800** | **0.771** | **0.599** | **0.475** | **0.424** |
| 20 — comparison | 0.900 | 0.925 | 0.883 | 0.900 | 0.817 | 0.768 | 0.584 | 0.488 |

Eight of the ten types the benchmark carries. The two missing ones are missing
because document recall is undefined for them, not because they are unflattering:
`high_level` (10 questions) and `info_not_found` (20) ship with no
`expected_doc_ids`, so there is no document to have retrieved. `info_not_found`
is scored instead as abstention, below. The recorded run is at
`artifacts/recall_by_budget.json`, which holds every budget and every metric
above, so these are checkable without re-running anything.

That 0.424 is the number graph reasoning has to move, and it is why the graph
exists rather than a bigger index.

**Evidence discipline, measured rather than asserted.** Of 505 claims the model
produced in a pilot batch, **62 (12%) cited evidence that does not appear
verbatim in the source and were rejected**. A span altering a single word of a
real sentence is refused, and so is one that changes only a quote mark or only
its capitalisation — `tests/test_validate_spans.py` holds twenty-six cases
against `llm.validate_spans`, including that an empty span cannot match every
document and that the offsets written onto `EvidenceSpan` index the span they
claim to.

**Ingestion**: ~19,000 nodes/second measured; the whole corpus normalises in 22
seconds and indexes in 312. Registering 511,958 document ids produced **zero
collisions**.

**Demo stability.** Ten consecutive rounds of the four demo questions — one
supported, one contested, one either, one unanswerable (`scripts/80_demo_check.py`).

It asserts invariants every round and *tallies* verdicts, because synthesis is
not deterministic and this check is what established that: a question answering
`supported` in thirty-nine rounds of forty came back `insufficient` once, and a
separate ablation found four verdicts in twelve moving between two runs of
identical code. Failing on that would measure the model's temperature. What
never varies, and is asserted absolutely: every citation exists in the graph
under a labelled match, an abstention carries no citations or claims, a
`conflicting` verdict names the rival version, and a `supported` answer is not
sitting on a dispute the system found. It also refuses to pass a run in which no
question came back contested, because a controller that had quietly stopped
detecting conflicts would otherwise score ten out of ten. Latency p50 8.6s, p95 14.5s.

`artifacts/demo_stability.json` records the run rather than summarising it: the
commit, both model ids, the run and read epoch, and all forty raw latency
samples, so the aggregates can be recomputed instead of taken on trust. Note
what "100%" means there — it is 100% *within each question's allowed set*, and
the artifact shows the split: Grace O'Connor came back `conflicting` eight times
and `insufficient` twice, both acceptable and both recorded.

That p50 was 29.6s until the corpus was re-chunked. The file ships as a single
row group holding all 511,962 documents, and parquet decodes a row group whole,
so fetching one document read the entire 1.4 GB file — 4.5 seconds, paid four
times per question because on-demand ingestion enriches several candidates at
once. Answering spent longer scanning parquet than talking to the model.
`scripts/71_repartition_corpus.py` writes a lossless copy at 2,048 rows per group
and indexes that: **4,465ms → 12ms per document fetch**, and the preload scan on
the first question disappears entirely.

**Two graph-retrieval features were built, measured, and removed.** Both worked
as queries; neither survived measurement, and the second failed in an
instructive way — the run-to-run noise floor of the system proved larger than
the effect being measured, which means a single-run A/B here cannot distinguish
a retrieval feature from model nondeterminism. The measurements, and what they
imply about evaluating this class of system, are in
[`docs/negative-results.md`](docs/negative-results.md).

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
git clone https://github.com/jeswinlobo/hydra-x-submission && cd hydra-x-submission
cp .env.example .env          # add ANTHROPIC_API_KEY
# The corpus, ~1.4 GB and Git LFS. Use the CLI rather than `git clone`: without
# `git lfs` installed a clone leaves a 130-byte pointer file at the right path,
# which passes bootstrap's existence check and then fails inside pyarrow minutes
# later. MIT licensed, not gated.
# `--with` pulls the CLI for this one command; it is not a project dependency.
# The entrypoint is `hf`, not the older `huggingface-cli`, which is deprecated.
uv run --with huggingface_hub hf download \
    onyx-dot-app/EnterpriseRAG-Bench --repo-type dataset \
    --local-dir dataset/EnterpriseRAG-Bench
#   -> dataset/EnterpriseRAG-Bench/data/{documents,questions}/test.parquet

./scripts/bootstrap.sh        # everything, in order, ~10 minutes
./scripts/60_serve.sh         # → http://127.0.0.1:8000
```

A question can go in the URL — `http://127.0.0.1:8000/?q=your+question`, with
`&tab=resolution` to open a panel — so a result is a link rather than an
instruction to type something, and a reload reproduces it.

`bootstrap.sh` checks its prerequisites before running anything slow, and every
stage is resumable, so it is safe to run twice. `--fast` skips the slice: questions
still work, because documents are enriched on demand, but the resolution and
conflict panels start empty.

`scripts/01_hydra_up.sh` does not report success on a listening port — it
round-trips a real query first, because a port is not proof.

## Verifying the claims above

```bash
uv run pytest                             # 305 tests, 27 against the live engine
uv run python scripts/35_verify_gate.py   # 14 checks, read back from the graph
uv run python scripts/36_repair_graph.py  # audit identities and undecided mentions
uv run python scripts/37_rebuild_resolution.py  # re-decide every identity, report drift
uv run python scripts/76_recall_by_budget.py  # recall at 4, 8 and 20, one pass
uv run python scripts/78_abstention_eval.py   # abstention, graph vs no-graph
uv run python scripts/79_backfill_tickets.py  # ticket graph coverage (report only)
```

Every command above is read-only *with respect to the answer key* — none of them
can see `gold_answer`. `78_abstention_eval.py` does enrich the graph, because
answering a question is what it measures; the rest change nothing.
`scripts/55_conflicts.py` is a pipeline step, not a check — it writes
`CONFLICTS_WITH` edges — so it lives in `bootstrap.sh` rather than here;
`36_repair_graph.py` likewise only writes with `--apply`.

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

`tests/test_hydra_contract.py` pins the first three, so an engine upgrade that
breaks one fails loudly rather than corrupting the graph quietly. The label-index
ceiling is not pinned — reproducing it means deliberately overflowing a label,
which leaves the store unrepairable, so it is documented rather than tested.

## Documentation

| | |
|---|---|
| `PLAN.md` | The execution plan this was built against |
| `docs/engine-notes.md` | Verified HydraDB behaviour and its constraints |
| `docs/negative-results.md` | Features built, measured, and removed, with the measurements |
| `docs/upstream-issues.md` | Two engine findings written up for the HydraDB tracker |
| `docs/corpus-notes.md` | What the corpus actually contains |
| `docs/source-notes.md` | Document shapes for all nine sources |
| `docs/refs.lock.md` | Pinned upstream commits and firewalled material |

## Licence and attribution

MIT (`LICENSE`). HydraDB is AGPL-3.0 and is used as an unmodified, separately
containerised service over its network APIs — no source is vendored or modified.
Full third-party notices in `NOTICE.md`.

Built for Hack Hydra 2026 by [Jeswin Lobo](https://github.com/jeswinlobo),
Sheldon Menezes and Stalin Prevan Crasta. Contributions are set out in
`SUBMISSION.md`, including which of them predate this branch's history.
