# Submission — Hack Hydra Track 1

Answers drafted for every field on the official form
(<https://forms.gle/GrMYKxLj9zPQcqqc8>), so submission is copy-paste.

**Deadline: 20 August 2026, 11:59 PM PT** — 21 August, 12:29 PM IST.

---

## Project name

TraceGraph

## Short description

An enterprise truth debugger on HydraDB. It answers questions over half a
million documents from nine business tools, resolves the same person across
`sam` / `Sam Carter` / `sam.carter@dataforge.ai`, keeps contradictory versions of
a fact instead of picking one silently, and abstains when the evidence does not
support an answer — citing the exact document and verbatim span behind every
claim it makes.

## The problem you are addressing

Enterprise knowledge is scattered across tools that disagree with each other,
and the disagreement is invisible. The same person appears under a handful of
identities; the same fact appears in several incompatible versions; a document
that was true last quarter still reads as authoritative today.

Search returns documents. It does not tell you whether the document is right,
who contradicts it, or how it knows — so the failure mode is a confident answer
built on a superseded source, which is worse than no answer.

Track 1 names the hard part exactly: entity resolution and ontology alignment.
In this corpus that is not incidental difficulty. Slack is 55.8% of the
documents and its speakers are bare first names; `sam` has sixteen plausible
referents and `alex` has forty-three. No amount of string similarity separates
them.

## What you built

A question-answering system over all 511,962 documents where the graph does the
reasoning and every claim is checkable.

- **Retrieval over the whole corpus.** Contentless FTS5 across every document,
  with the index rowid being the document's deterministic 63-bit graph id, so a
  hit is already a node.
- **On-demand enrichment.** Whatever retrieval reaches is parsed, resolved, and
  extracted into HydraDB during the request, and stays for later questions — the
  graph grows toward what people actually ask about.
- **Entity resolution decided by traversal**, not by string similarity. See the
  HydraDB answer below.
- **Provenance as structure.** Claims and evidence spans are nodes, so a span can
  be inspected alone and one span can support several claims. A claim whose span
  is not verbatim in its source is never written.
- **Conflict detection with decomposed trust.** Contested facts become
  `CONFLICTS_WITH` edges. Every version is kept with its own authority,
  corroboration, directness, and recency, and a winner is named only when the
  evidence justifies one.
- **A deterministic controller.** The model writes prose; it does not decide what
  is true, what may be cited, or whether a question is answerable. Each of those
  is settled by a check that can fail.
- **Ask & Inspect**, a single page over a live API — answer, citations, evidence
  subgraph, resolution panel, conflict panel, and the engine's real `read_epoch`.

## How your project uses HydraDB

HydraDB decides things. It is not a store results are filed into afterwards, and
three capabilities disappear entirely without it.

**1. Entity resolution by graph traversal.** Participation is written to the
graph first — who speaks in which channel, which mention sits in which document
— and ambiguous surfaces are then scored by traversals over that structure:
co-occurrence in the same document at two hops, participation in the same
channel at one. On the loaded slice this decided **589 surfaces across 4,869
queries to the engine**, including `alex` → Alex Chen over 42 competing
candidates. Where the graph does not separate candidates, the mention stays
unresolved with its candidate set recorded as `CANDIDATE_FOR` edges.

**2. Provenance and conflict topology.**
`Document -[:ASSERTS]-> Claim -[:SUPPORTED_BY]-> EvidenceSpan`, with
`CONFLICTS_WITH` between contested claims, so the answer path finds contradictions
by traversal rather than recomputation.

**3. Native path procedures and real consistency evidence.** `algo.SPpaths`,
`algo.SSpaths`, and `algo.MSpaths` return whole evidence paths, which is what the
interface renders. Every answer carries the engine's own `read_epoch` and
bookmark — read off the response, never composed.

The engine also shaped the design rather than merely hosting it. A bare
`{id: N}` pattern is an address lookup, not an existence check, so citation
validation must use a labelled match or it is vacuously true. A label index
holds at most 250,000 vertices, which is why corpus scale lives in the lexical
index and the graph holds the enriched working set. Both are recorded in
`docs/engine-notes.md` with the probes that produced them, and pinned by tests.

**Without HydraDB** this degrades to a document retriever with no explainable
identity decisions, no traversable claim lineage, and no conflict graph.

## Tech stack

Python 3.12 · HydraDB (unmodified, pinned by digest, over Bolt and its HTTP
query API) · Neo4j Bolt driver · SQLite FTS5 · PyArrow · Anthropic Claude API
(`claude-haiku-4-5-20251001` for extraction, `claude-sonnet-5` for synthesis) ·
FastAPI · uv · pytest.

## Results

Retrieval over all 511,962 documents against the 470 benchmark questions that
carry an answer key: **mean document recall 0.736**, at least one expected
document for **77.4%** of questions, median rank of the first correct document
**1**. By type it ranges from 0.925 on intra-document reasoning down to 0.488 on
semantic questions — that gap is precisely what graph reasoning exists to close.

Evidence discipline is measured, not asserted: of 505 claims produced in a pilot
batch, **62 (12%) cited evidence not appearing verbatim in the source and were
rejected**.

Ingestion runs at ~20,000 nodes/second; registering 511,958 document ids produced
zero collisions. 103 tests pass, 18 of them against the live engine.

## Team members and contributions

Jeswin Lobo — sole participant. Architecture, implementation, evaluation, and
write-up. Built with AI coding assistance (Claude), which the rules permit and
which is credited here and in the commit history.

## Links

| | |
|---|---|
| GitHub | https://github.com/jeswinlobo/hydra-x |
| Demo video | *(add before submitting)* |
| Deployed | none — runs locally against a pinned HydraDB container |

---

## Pre-submission checklist

Every item is a listed disqualification reason. Tick them off in order.

- [x] Open-source licence in the repository — MIT, `LICENSE`
- [x] Third-party notices — `NOTICE.md` (HydraDB AGPL-3.0, unmodified, over the network)
- [x] README explains the project clearly
- [x] Setup instructions verified from a fresh clone
- [x] HydraDB usage clearly explained
- [x] No secrets, datasets, database state, or reference clones committed
- [x] No participant-authored commits before 12 August 2026
- [ ] **Repository is public** — currently private; flip before submitting
- [ ] Demo video, 3 minutes or less, link opens in an incognito window
- [ ] Submission form complete
- [ ] Team members listed correctly
- [ ] Submitted before 11:59 PM PT on 20 August

## Video plan — 2:55, in the order the guide mandates

| Time | Beat |
|---|---|
| 0:00–0:20 | **The problem.** Nine tools, half a million documents, one person under three names, one fact in four versions. |
| 0:20–0:35 | **The project.** TraceGraph: answers with citations, kept contradictions, honest abstention. |
| 0:35–1:00 | **Demo.** Ask a real question; grounded answer with exact dsid citations, evidence subgraph. |
| 1:00–1:30 | **Demo.** Resolution panel: `alex` against 43 candidates, decided by shared participation — show the query and the graph evidence. |
| 1:30–1:55 | **Demo.** Conflict panel: one person, several job titles, trust breakdown, recency-as-supersession. |
| 1:55–2:10 | **Demo.** Ask something the corpus does not contain; explicit abstention, no citations. |
| 2:10–2:55 | **HydraDB.** Ontology, `algo.MSpaths`, bounded hops, the real `read_epoch` in the trace, and one sentence on what disappears without it. |

Record only after ten consecutive clean demo runs. Curated questions are fine;
hard-coded answers are not.
