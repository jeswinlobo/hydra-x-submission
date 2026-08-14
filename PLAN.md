# Hack Hydra — Track 1: 10/10 Target Execution Plan

## Outcome

Build a working enterprise truth debugger on HydraDB: a product that turns roughly 512,000 noisy documents from nine enterprise sources into a provenance-first ontology, resolves ambiguous identities and relation vocabulary, preserves conflicting claims, answers multi-hop questions with exact citations, and abstains when evidence is insufficient.

Working name: **TraceGraph**. Freeze the final name by Aug 15; do not spend engineering time on branding before the core vertical slice works.

One-line pitch:

> TraceGraph does not merely retrieve similar documents. It shows which enterprise entities and claims are connected, which sources support or contradict them, and the exact HydraDB evidence path behind every answer.

HydraDB is used as an external database service through its published Docker image, Bolt, and HTTP APIs. We do not modify or vendor HydraDB's Rust source. The local hydradb/ clone is reference material and must not be committed to the project repository.

No plan can guarantee a win. This plan targets a 9/10–10/10 submission by producing visible proof for every judging criterion while keeping the scope achievable before Aug 20, 2026.

## What judges must be able to prove

| Criterion | Judge-visible proof |
|---|---|
| Technical execution | Reproducible ingestion, deterministic IDs, tested HydraDB queries, verified evidence spans, working UI |
| Graph-native HydraDB use | Entity-resolution neighborhoods, bounded multi-hop paths, provenance traversal, conflict graph, and a real epoch or durable bookmark from the chosen query transport |
| Product completeness | One polished Ask & Inspect workflow, citations, explanations, sample quickstart, optional hosted sample |
| Quality of results | Hybrid baseline, graph-enabled result, category evaluation, calibrated abstention |
| Originality | Reversible resolution and temporal claim adjudication rather than another generic RAG chatbot |
| Best Use of HydraDB | A typed ontology and reasoning flow whose hard capabilities disappear if HydraDB is removed |

Every important README or video claim must be backed by a live interaction, HydraDB trace, integration test, or recorded metric.

## Scope discipline

### P0 — must ship

- Public project repository with safe gitignore, open-source license, attribution, and no pre-Aug-12 participant work.
- Pinned HydraDB container, health check, and automated Bolt/HTTP round-trip proof.
- Full-corpus document registry and lexical discovery for all nine sources. Rich P0 structure edges are limited to Slack, Gmail, GitHub, Linear, and Jira; Drive, Confluence, Fireflies, and HubSpot receive document nodes, full-text retrieval, exact references, and priority claim extraction before broader source-specific structure.
- Correct ontology containing entities, mentions, predicates, claims, values, evidence spans, and provenance.
- Deterministic node and relationship IDs with collision detection.
- Hybrid candidate retrieval; HydraDB performs evidence composition and reasoning.
- Reversible entity resolution using graph evidence and explicit unresolved states.
- Temporal and scope-aware conflict detection preserving every supported alternative.
- Deterministic answer controller using bounded, tested HydraDB query templates.
- Exact dsid citations, answerability verdict, confidence explanation, and calibrated abstention.
- One polished Ask & Inspect UI with focused evidence graph and HydraDB trace.
- Three excellent demos: entity resolution/multi-hop, conflict, and abstention.
- Baseline-versus-graph ablation and reproducible sample dataset.
- README, setup, video, and submission form completed by Aug 19.

### P1 — only after P0 works end to end

- Broader structured extraction across all nine sources.
- Tiered LLM claim extraction beyond high-value and query-reached documents.
- Complete embedding coverage.
- Full 500-question proxy evaluation and category failure analysis.
- Read-only hosted sample.
- Small HERB evaluation appendix.

### Explicitly defer if schedule slips

- Four separate UI pages.
- Topic clustering.
- Full-corpus LLM extraction.
- Full HERB benchmark.
- Corpus-wide MinHash before exact hashing and retrieval work.
- Complex distributed HydraDB/indexer deployment.
- Centrality-heavy truth scoring.
- Graph mutations during an answer.

## Verified source and environment facts

- Primary corpus: dataset/EnterpriseRAG-Bench/, about 1.4GB and 511,962 documents across Slack, Gmail, Linear, Drive, HubSpot, Fireflies, GitHub, Jira, and Confluence.
- Primary evaluation: 500 questions across ten categories.
- The local dataset checkout contains the data card and Parquet files, not the official evaluator implementation. Evaluation code must be acquired separately or reproduced with a documented adapter.
- The documented Parquet schema contains doc_id, source_type, title, and content. Authors, timestamps, threads, recipients, and other structure must be verified inside actual content before parser assumptions are made.
- Secondary dataset: dataset/HERB/, CC-BY-NC-4.0. Use only for isolated research evaluation. Never use its team and customers oracle fields as retrievable evidence.
- Local machine: 16 cores, 15GB RAM, RTX 3050 4GB, Docker available, about 18GB free disk.
- LLM: self-hosted gpt-oss-120b on a DigitalOcean AMD GPU droplet through vLLM's OpenAI-compatible API.
- Deadline: Aug 20, 2026. Target completed submission: Aug 19.

## HydraDB constraints controlling the design

hydradb/cypher-compat.md, hydradb/README.md, and hydradb/AGENTS.md are the sources of truth.

| HydraDB behavior | Required response |
|---|---|
| Node IDs are non-negative integers and identity matches by id | Use deterministic 63-bit IDs with a collision registry |
| Relationships can carry identity | Assign a deterministic ID to every relationship |
| Properties are int, float, bool, or string | Represent aliases, values, lists, and evidence as nodes/edges; timestamps use epoch integers |
| MERGE has no ON CREATE or ON MATCH | Upsert vertices by ID then SET; checkpoint batches because no-op MERGE still commits |
| Batched UNWIND works through client transport | Use Neo4j Python driver over Bolt; one directed relationship type per batch |
| Relationship patterns are directed and single-typed | Use explicit directions and a documented convention for symmetric relations |
| WITH is pass-through only; one statement per request | Compose steps in a deterministic application controller |
| Variable paths must be bounded | Product hop limits are 1–3 with explicit result budgets |
| Native SPpaths, SSpaths, and MSpaths return paths | Use them for evidence paths and multi-entity reasoning |
| Each query pins one snapshot | Carry causal bookmarks and show epochs; do not call a multi-query answer one snapshot |
| Query budgets are enforced | Avoid hub explosions, paginate, and use narrow typed traversals |
| latest can move | Verify with latest, then pin a release tag or digest |

Identity rules:

    node_id = hash(node_type, canonical_natural_key) masked to 63 bits
    edge_id = hash(edge_type, source_id, target_id, scope_or_evidence_id) masked to 63 bits

Use SHA-256 and retain the full hash in a SQLite collision registry containing ID, type, natural key, and full hash. A collision fails loudly. The registry is a correctness component, not merely a debugging aid.

## Winning architecture

    EnterpriseRAG-Bench Parquet
            |
            +--> source-aware parser --> document/entity/reference batches
            +--> contentless FTS5 -----> lexical candidate discovery
            +--> bounded embeddings ---> semantic candidate discovery
                                            |
                                            v
                                    Candidate seed set
                                            |
                                            v
    HydraDB <--- deterministic Bolt ingestion --- extraction / ER / alignment
       |
       +--> enterprise structure
       +--> canonical entities and ontology
       +--> claims, values, evidence, provenance
       +--> conflict/corroboration/supersession edges
       +--> native bounded paths
                    |
                    v
            Deterministic answer controller
                    |
                    +--> evidence-bounded LLM synthesis
                    +--> exact citation validation
                    +--> confidence and abstention
                                |
                                v
                      FastAPI + Ask & Inspect UI

Division of responsibility:

- Parquet remains the authoritative full-text store; do not duplicate every body into another large database.
- Contentless FTS5 and a compact embedding index discover candidate documents and aliases.
- HydraDB stores the ontology, canonical entities, graph structure, claims, truth-maintenance edges, and traversal evidence.
- The LLM extracts and synthesizes but cannot invent graph IDs, claims, evidence, or citations.
- The controller validates every citation and evidence span against source text.
- Sidecars and HydraDB share a versioned ingestion_run_id, corpus fingerprint, graph bookmark, and index manifest so they cannot silently drift.

Hybrid retrieval is mandatory. It finds entry points; HydraDB performs the relationship reasoning that turns those candidates into an answer.

## Ontology and provenance model

### Core nodes

- Entity: common base label with kind such as person, team, product, project, company, channel, ticket, PR, repo, meeting, or topic.
- Class: ontology classes.
- Document: dsid, source type, title, timestamp if present, content hash, extraction state, ingestion version.
- Mention: exact surface text, normalized surface, start and end offsets.
- Alias: normalized lookup key.
- Predicate: canonical relation, mutability flag, domain, range, and authority profile.
- RawPredicate: source phrase before ontology alignment.
- Claim: indexed subject_id, predicate_id, claim_group_id, asserted time, validity interval, scope, modality, extraction confidence, resolution confidence, trust score, and status.
- ClaimGroup: deterministic group_key for canonical subject plus predicate plus normalized scope. This is the bounded unit for claim lookup, conflict detection, completeness, and adjudication.
- Value: typed scalar object for claims whose object is not an entity.
- EvidenceSpan: document ID, offsets, and quote hash.

All traversable domain objects use a common Entity label where native path lookup requires a shared label. Kind or supported additional labels retain subtype.

### Ontology edges

- INSTANCE_OF: entity to class.
- SUBCLASS_OF: class to class.
- MAPS_TO: raw predicate to canonical predicate.
- DOMAIN: predicate to valid subject class.
- RANGE: predicate to valid object class or value type.

### Provenance and resolution edges

- MENTIONED_IN: mention to document.
- RESOLVES_TO: mention to canonical entity, with confidence and method.
- HAS_ALIAS: entity to alias.
- ASSERTS: document to claim.
- IN_CLAIM_GROUP: claim to claim group.
- ABOUT_SUBJECT: claim group to canonical subject entity.
- USES_PREDICATE: claim group to canonical predicate.
- HAS_OBJECT: claim to entity or value.
- SUPPORTED_BY: claim to evidence span.

Do not leave claim objects as object_repr strings. Entity and Value targets must remain traversable.

ClaimGroup prevents every Claim from fanning into a handful of global Predicate supernodes. Query claim groups by their exact indexed group_key, and retain subject_id and predicate_id scalar properties on Claim for indexed filtering and debugging. Generic path procedures must use an explicit relationship-type allowlist and must not cross ontology metadata edges such as USES_PREDICATE unless the operation specifically requests ontology traversal.

Keep EvidenceSpan as a first-class, normally low-degree node because one span can support multiple claims and the UI needs independently verifiable provenance. Consider offsets on SUPPORTED_BY edges only if a measured ingestion or query bottleneck justifies that denormalization.

### Enterprise structure edges

AUTHORED, SENT_IN, REPLIED_TO, PARTICIPATED_IN, MEMBER_OF, WORKS_ON, ASSIGNED_TO, REVIEWED, BELONGS_TO, REFERENCES, DUPLICATE_OF.

### Truth-maintenance edges

- CONFLICTS_WITH: incompatible claims in overlapping scope and validity.
- SUPERSEDES: later authoritative claim replaces an older mutable claim.
- CORROBORATES: independent sources support the same normalized claim.
- DERIVED_FROM: copied evidence traces to a common source and cannot be double-counted.

CONFLICTS_WITH is logically symmetric, while HydraDB edges are directed. Store one deterministic edge from lower claim ID to higher claim ID and make the API query both directions, or store two deterministic directed edges. Choose one convention once and cover it with tests.

## Ingestion and enrichment

### Stage 0 — inspect before assuming

- Read representative documents from every source.
- Document actual source templates and reliable fields.
- Create parser fixtures for at least three samples per source.
- Send malformed records to a resumable error queue.

### Stage 1 — complete discovery layer

- Stream every document from Parquet.
- Create deterministic Document nodes.
- Build doc_id to Parquet row-group/row lookup metadata.
- Build contentless FTS5 over title and content.
- Build compact embeddings in bounded batches, starting with titles and source-aware chunks.
- Record index versions and disk usage.

### Stage 2 — high-precision structure

- Extract only verified fields: authors, recipients, timestamps, channels, threads, ticket keys, PR links, URLs, mentions, and explicit references.
- Prefer missing a weak edge over creating a false one.
- Ingest endpoint nodes before relationships.
- Group UNWIND batches by node label or relationship type.
- Benchmark 1, 2, and 4 Bolt sessions rather than assuming parallelism helps.

### Stage 3 — duplicates and lineage

- Start with exact and normalized-content hashes.
- Add streaming SimHash or MinHash only when needed.
- Connect copied content with DUPLICATE_OF or DERIVED_FROM so it does not count as independent corroboration.

### Stage 4 — tiered LLM extraction

- Use strict JSON Schema with bounded retries and failure logs.
- Every claim must contain a verbatim evidence span validated against the document.
- Use source-aware chunks with overlap; do not blindly truncate documents.
- Never expose gold answers, answer facts, or expected document IDs to extraction.
- Checkpoint by document hash, prompt version, and model version.

Question-independent extraction order:

1. Drive, Confluence, Fireflies, HubSpot, profiles, and highly referenced documents.
2. Long Gmail threads and high-degree Slack threads.
3. Documents reached during normal user retrieval.

Lazy extraction is queued after an answer. It must not mutate the graph in the middle of a reasoning trace. The UI may offer a re-run after enrichment commits.

### Stage 5 — entity resolution

1. Resolve strong keys: employee/customer IDs, emails, stable handles, ticket and repository IDs.
2. Block by normalized names, handle stems, email local parts, initials, organizations, and kinds.
3. Score string similarity and compatible attributes.
4. Apply cannot-link rules for conflicting stable IDs, incompatible kinds, and clearly different organizations.
5. Add HydraDB evidence from shared channels, threads, projects, co-authorship, references, and bounded paths.
6. Send only the small ambiguous remainder to structured LLM adjudication.
7. Store RESOLVES_TO confidence and method; never delete mentions.
8. Preserve unresolved rather than forcing a weak match.

The demo must show the neighborhood or path that made one identity candidate stronger than another.

### Stage 6 — ontology alignment

- Seed a small canonical predicate catalog with domains, ranges, mutability, and authority profiles.
- Map raw predicates using lexical similarity, domain/range compatibility, and graph neighborhoods.
- Use the LLM only as an ambiguous mapping tiebreaker.
- Store every RawPredicate to MAPS_TO to Predicate decision and confidence.
- Route unknown relations to an unmapped queue instead of inventing categories.

### Stage 7 — conflicts and trust

Two claims conflict only when they share canonical subject and predicate, overlap in scope and validity, and have incompatible normalized objects.

Trust components:

- Predicate-specific system-of-record authority.
- Direct assertion versus hearsay or copied content.
- Relevant author role, not generic popularity.
- Extraction and resolution confidence.
- Independent corroboration after duplicate discounting.
- Recency only for mutable predicates.
- Explicit approval, merge, closure, or supersession signals.

Graph centrality can be a tiny tiebreaker but must never dominate source-of-record evidence. Store each score component so the UI can explain it.

Do not hide the lower-ranked claim. Conflict answers return all materially supported versions, citations, temporal context, and only when justified the best-supported current interpretation.

## Answer controller

Use a deterministic controller with typed query plans. The LLM may classify a question and fill parameters, but it does not invent arbitrary Cypher.

| Question type | Strategy |
|---|---|
| Basic | Hybrid document seed, exact evidence, optional one-hop context |
| Semantic | FTS/embedding fusion, entity resolution, evidence reranking |
| Intra-document | Retrieve chunks, fetch adjacent/full sections, synthesize |
| Project related | Resolve project/entities, bounded artifact and participant expansion |
| Constrained | Parse qualifier, then apply source/time/entity/property constraints |
| Conflicting | Retrieve the complete canonical claim group and provenance branches |
| Completeness | Expand related evidence until a documented coverage rule is met |
| High level | Collect multiple claim groups and summarize corroborated themes |
| Miscellaneous | Hybrid retrieval with conservative graph expansion |
| Info not found | Evidence sufficiency test followed by explicit abstention |

Tested backend operations:

- resolve_entity
- retrieve_candidates
- entity_neighborhood with max_hops no greater than 3
- paths using SPpaths or MSpaths
- claim_group
- evidence_for_claims
- fetch_documents

Answer contract:

    {
      "answer": "...",
      "document_ids": ["dsid_..."],
      "answerability": "supported | conflicting | insufficient",
      "confidence": 0.0,
      "claims": [],
      "alternatives": [],
      "hydradb_trace": {
        "queries": [],
        "procedures": [],
        "hops": 0,
        "consistency": {
          "transport": "http | bolt",
          "read_epoch": null,
          "bookmark": "...",
          "storage_sequence": null
        },
        "latency_ms": 0
      }
    }

Before returning:

- Every document ID exists.
- Every evidence span matches source content.
- Every answer fact points to retrieved evidence.
- Conflict alternatives are not silently discarded.
- Insufficient support causes abstention, not model-memory completion.

## Consistency

- Use Bolt for batched ingestion. Prefer the HTTP JSON/NDJSON query API for judge-visible reads because its response schema includes read_epoch and bookmark.
- Treat read_epoch as optional until the exact pinned container is integration-tested. Standard Bolt clients expose the durable bookmark publicly while HydraDB keeps the read epoch internally for execution and tracing.
- If read_epoch is unavailable, display the bookmark. Show its storage sequence only when a tested parser has validated the bookmark format; never render a fabricated zero epoch.
- Carry causal bookmarks between controller operations.
- Do not describe a multi-query run as a single snapshot.
- Do not write during an answer trace.
- Queue enrichment and expose a new graph version after commit.
- Return ingestion version in debug traces.

## Product: one polished Ask & Inspect experience

The interface contains:

1. Question input with curated examples.
2. Streamed answer and exact dsid citations.
3. Answerability and explained confidence.
4. Focused evidence subgraph, never an uncontrolled hairball.
5. Entity-resolution panel with aliases, candidates, scores, and decisive paths.
6. Conflict panel with versions, validity, source authority, and trust breakdown.
7. HydraDB trace with transport, query/procedure, relationship types, hop bound, latency, result count, and the real epoch or bookmark/storage sequence exposed by that transport.
8. Compact graph status showing document, entity, claim, and edge counts plus ingestion version.

Targets:

- Warm end-to-end response below 12 seconds for demo questions.
- HydraDB graph-query p95 below 1 second for demo paths.
- Ten consecutive successful curated demo runs.
- Warm caches are acceptable; hard-coded answers are not.

## Evaluation

### Development hygiene

- Shallow-clone the upstream EnterpriseRAG-Bench repository into a gitignored external/reference directory, record the exact upstream commit, inspect answer_evaluation/README.md, and run both evaluator commands with --help.
- Keep our answer JSONL adapter in the project repository. Its output must contain question_id, answer, and document_ids exactly as expected upstream.
- Run a one-question evaluator smoke test before depending on it for variants A–D; evaluator dependencies, credentials, resume files, and judge choice must be explicit.
- Create a seeded, stratified 100-question development set.
- Keep 400 questions locked until thresholds and pipeline are frozen.
- Ingestion and answering never read gold answers, answer facts, or expected document IDs.
- Record split seed, corpus fingerprint, prompts, model, index manifest, and graph version.
- Run the full 500 questions after freezing configuration.

### Compare four systems

| Variant | Purpose |
|---|---|
| A. FTS only | Minimal lexical baseline |
| B. Hybrid retrieval | Strong non-graph baseline |
| C. Hybrid plus HydraDB structure | Measures entity resolution and graph expansion |
| D. Full TraceGraph | Adds ontology, claims, conflicts, trust, and abstention |

Report official EnterpriseRAG-Bench metrics only when using the official evaluator:

- Overall correctness times completeness.
- Answer correctness.
- Answer completeness.
- Document recall.
- Invalid extra documents.
- Category results.

Scores judged internally by gpt-oss-120b are proxy evaluation, never official GPT-5.4 leaderboard scores.

Additional metrics:

- Citation validity.
- Evidence-span grounding.
- Entity-resolution pairwise precision/recall and false merges.
- Ontology-mapping accuracy on a reviewed set.
- Conflict precision.
- Abstention false-answer rate, coverage, and selective accuracy.
- Retrieval, HydraDB, LLM, and total p50/p95 latency.
- Replay/resume idempotency.

Target gates, not fabricated results:

- 100% valid returned citations.
- 100% accepted claims have verified evidence spans.
- Zero ID collisions and duplicate edges after replay.
- Entity-resolution precision at least 98% on a reviewed suite; precision outranks recall.
- Conflict precision at least 85% before showing automatic winners.
- Abstention false-answer rate no more than 10% on calibration data.
- Positive graph uplift over hybrid on project, conflict, completeness, and constrained categories.
- Ten consecutive demo runs without unhandled errors.

### HERB

HERB is optional secondary evidence:

- Use a small subset for employee-ID resolution and abstention.
- Exclude team and customers oracle fields from retrieval.
- Keep HERB out of the public sample unless licensing permits.
- Attribute CC-BY-NC-4.0 and the dataset restrictions.
- Drop HERB entirely before cutting primary-corpus work.

## Why HydraDB is indispensable

HydraDB performs real work in five visible places:

1. Stores the enterprise ontology, artifacts, claims, evidence, and truth-maintenance relationships.
2. Produces graph-neighborhood evidence for ambiguous entity resolution.
3. Executes bounded multi-hop reasoning with native path procedures.
4. Traverses provenance branches for conflicts, corroboration, and supersession.
5. Returns durable epochs/bookmarks used in answer traces.

External search finds starting points. It does not replace entity resolution, ontology alignment, provenance paths, conflict topology, or multi-hop context assembly.

Without HydraDB, the project degrades into a conventional document retriever with no explainable identity decisions, traversable claim lineage, native relationship reasoning, or evidence graph.

## Verification

### HydraDB contract tests

- Container reaches ready state.
- HTTP write/read returns the expected vertex.
- Bolt verifies connectivity and result.
- Every production Cypher template runs against the pinned image.
- Node and relationship batches replay without count changes.
- Directed conflict convention works in both logical directions.
- Path queries stay within bounds and paginate or fail clearly.
- Bookmarks propagate across controller steps.

### Pipeline tests

- Parser fixtures for every source.
- Stable IDs and a forced-collision test.
- Interrupted ingestion resumes from the last committed batch.
- Text and graph manifests share one ingestion version.
- Exact evidence-quote validation.
- Gold fields are inaccessible to ingestion and answering.
- No secrets, database state, full datasets, or HydraDB source enter project Git status.

### Product tests

- Golden smoke questions covering all ten categories.
- At least 100 reviewed identity cases including strong, ambiguous, and unresolved examples.
- Reviewed predicate-mapping and conflict sets.
- Ten consecutive live demo runs.
- Fresh-clone sample quickstart before recording.

## Six-day execution schedule

### Aug 14 — safe repository and vertical slice

- Initialize the project repository immediately; the workspace root is not currently a Git repository.
- Ignore hydradb/, full datasets, database data, indexes, caches, logs, secrets, and checkpoints.
- Add license and attribution skeleton.
- Start HydraDB and prove HTTP/Bolt write/read.
- Inspect at least one real document from all nine sources, but implement only the parser needed by the representative vertical slice tonight.
- Build a slice-level FTS retrieval path sufficient for the vertical slice; full-corpus FTS belongs to Aug 15.
- Implement ID registry and minimal Bolt loader.
- Ingest a representative slice.
- Answer one real question with a valid citation and HydraDB trace.
- Run a vLLM connectivity and one-document structured-JSON smoke test. Move the throughput benchmark to Aug 15 if time is short.
- Shallow-clone and pin the EnterpriseRAG-Bench evaluator reference; verify its command surface. A full evaluator run is not a day-one gate.

Exit gate: one reproducible question flows from candidate retrieval through HydraDB evidence to a grounded answer. Do not start full-corpus extraction before this works.

Friday-evening cut order: defer the full vLLM throughput benchmark first, full parser fixtures second, and full-corpus FTS third. Keep a slice-level retrieval path, HydraDB round trip, ID/loader path, and one grounded vertical-slice answer. Those are the non-negotiable day-one proof.

### Aug 15 — complete discovery and structural graph

- Register all 511,962 documents.
- Build the full lexical index and bounded embedding batches.
- Benchmark vLLM tokens/sec and documents/hour before fixing extraction tiers.
- Complete rich structural parsers for Slack, Gmail, GitHub, Linear, and Jira.
- For Drive, Confluence, Fireflies, and HubSpot, ingest document nodes and exact references, then prioritize them for claim extraction; defer exhaustive source-specific structural edges to P1.
- Ingest high-confidence entities and references from the P0 parser set.
- Finish relationship IDs and checkpoint/resume.
- Benchmark 100k nodes/edges, batch sizes, sessions, disk growth, and query budgets.
- Create the 100/400 evaluation split.
- Implement the upstream answer JSONL adapter and pass a one-question evaluator smoke test.

Exit gate: all documents are discoverable, replay is idempotent, the ingestion benchmark is recorded, and disk remains within budget.

### Aug 16 — resolution, ontology, and claims

- Implement strong-key and blocked entity resolution.
- Add cannot-link rules and HydraDB graph evidence.
- Add Class, Predicate, ClaimGroup, Claim, Value, and EvidenceSpan schema.
- Implement raw-to-canonical predicate alignment.
- Run tier-1 extraction with evidence validation.
- Add duplicate/source-lineage discounting.
- Produce the entity-resolution/multi-hop demo.

Exit gate: entity resolution and ontology alignment are stored in HydraDB and explainable from evidence.

### Aug 17 — conflict and answer quality

- Implement temporal/scope-aware conflict detection.
- Add predicate authority profiles.
- Implement typed controller and query templates.
- Add bounded synthesis, citation validation, and abstention.
- Run variants A–D on the development set.
- Fix the weakest high-value category instead of adding features.
- Freeze answer schema and prohibit mid-answer writes.

Exit gate: all three demo scenarios work through the real pipeline and graph use shows measurable value over hybrid retrieval.

### Aug 18 — product and final evaluation

- Build unified Ask & Inspect UI.
- Add evidence graph, resolution panel, conflict panel, and HydraDB trace.
- Run locked evaluation and final 500-question configuration.
- Run ablations and latency tests.
- Build the public sample and optional hosted demo.
- Write README from actual implementation, not planned features.

Exit gate: stable UI, recorded metrics, fresh sample quickstart, and no unimplemented README claims.

### Aug 19 — submission freeze

- Pin HydraDB digest and dependency versions.
- Run fresh-clone verification.
- Complete README, setup, attribution, license, limitations, evaluation, and the without-HydraDB section.
- Document team contributions.
- Record the three-minute video.
- Submit form, repository, and video.
- Make no architectural changes after validation.

### Aug 20 — contingency only

- Verify submission links and visibility.
- Fix only disqualifying or demo-blocking issues.
- Do not leave evaluation, recording, or repository cleanup for this day.

## Decision gates and fallbacks

| Risk signal | Decision |
|---|---|
| vLLM throughput is below budget | Reduce extraction breadth; keep full retrieval coverage |
| Embeddings are incomplete by Aug 16 | Ship complete FTS plus embeddings for titles/high-value chunks and disclose coverage |
| HydraDB store threatens disk budget | Keep all metadata/structure, reduce claim coverage, never duplicate bodies |
| A source parser is unreliable | Store documents and only high-confidence references |
| Parallel ingestion is slower | Use the best measured session count and safe larger batches |
| Conflict precision is below 85% | Label potential conflicts and do not declare a winner |
| Graph does not beat hybrid | Repair high-value graph categories before UI extras |
| Hosted demo is unstable | Prioritize recorded demo and flawless sample quickstart |
| Schedule slips | Drop HERB, topic clustering, full extraction, extra screens, then hosting |

## Disk and operational budget

- Keep Parquet as body storage.
- Do not materialize 512k text files.
- Prefer contentless FTS.
- Cap and record embedding/index sizes.
- Monitor HydraDB store, cache, checkpoints, model cache, and Docker usage daily.
- Store only compressed structured extraction outputs and hashes.
- Preserve at least 4GB emergency free space.

## Submission deliverables

- Public GitHub repository containing only project code and permitted sample data.
- No participant-authored commits before Aug 12, 2026.
- MIT or Apache-2.0 license for the independent client project with careful third-party notices; do not present this as legal advice.
- HydraDB attribution and explanation that the unmodified AGPL service is used over network APIs.
- EnterpriseRAG-Bench MIT attribution.
- HERB CC-BY-NC attribution if HERB results are published.
- Docker Compose quickstart plus sample ingestion and demo commands.
- Environment example with no secrets.
- Pinned HydraDB and dependencies.
- Architecture and ontology diagrams matching shipped code.
- Proxy and official evaluation clearly separated.
- Honest limitations.
- Optional deployed sample.
- Three-minute video and completed form.

Never commit hydradb/, full datasets, database data, auth tokens, environment files, generated indexes, model artifacts, or evaluation secrets.

## Three-minute demo

- 0:00–0:20 — Problem: scattered, aliased, duplicated, contradictory enterprise facts.
- 0:20–0:55 — Ask a real question; show grounded answer and citations.
- 0:55–1:25 — Reveal the HydraDB path resolving an ambiguous identity and connecting evidence.
- 1:25–1:55 — Show conflicting versions, validity/source breakdown, and justified interpretation.
- 1:55–2:15 — Ask an unanswerable question and show evidence insufficiency.
- 2:15–2:40 — Show hybrid baseline versus graph result and one ablation.
- 2:40–3:00 — Show ontology, native path procedure, bounded hops, epoch, and what disappears without HydraDB.

Choose stable real examples after the pipeline finds them. Curated questions are acceptable; hard-coded answers are not.

## Definition of done

The project is ready only when:

- Fresh clone runs the sample in minutes.
- HydraDB passes HTTP and Bolt round trips.
- All production Cypher templates pass against the pinned image.
- Every citation is a valid dsid.
- Every accepted claim has verified evidence.
- Resolution, alignment, conflicts, and paths are queryable in HydraDB.
- UI shows the actual evidence graph and HydraDB trace.
- System returns supported, conflicting, or insufficient instead of forcing an answer.
- Baselines and ablations are recorded honestly.
- Ten consecutive demo runs succeed.
- Repository, README, setup, license, attribution, video, and form meet requirements.
- No secrets, full datasets, database state, indexes, or HydraDB source are committed.

## Confirmed decisions

1. Track: Enterprise Context & Ontology.
2. HydraDB: external unmodified Docker service through Bolt/HTTP.
3. LLM: self-hosted gpt-oss-120b on DigitalOcean AMD GPU through vLLM.
4. Primary corpus: EnterpriseRAG-Bench.
5. Deadline: Aug 20, 2026; target submission Aug 19.
6. Product: web UI with focused interactive evidence graph.
7. Positioning: enterprise truth debugger, not generic chat with documents.
