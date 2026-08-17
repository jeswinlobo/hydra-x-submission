# HydraDB engine notes — verified against the pinned image

Everything here was executed against
`ghcr.io/hydra-db/hydradb@sha256:db78309a2…` (upstream commit
`6a2fbb192f37f51a93690a2ae2d2f5e27e6e4219`) rather than read off documentation.
Where the shipped docs and the running engine disagree, the engine wins and the
disagreement is called out. `tests/test_hydra_contract.py` reproduces these
findings against a live node — 18 tests, run with `-m live` — and is the
evidence for everything below that the code depends on.

## Transports are not interchangeable

| | Bolt `bolt://127.0.0.1:7687` | HTTP `:8443/v1/graphs/default/query` |
|---|---|---|
| Auth | `auth=("neo4j", <token>)`, `database="default"` | `Authorization: Bearer <token>` + `X-Graph-Namespace: default` |
| `UNWIND` batches | **Yes** | No — list-of-maps parameters are a transport-level type |
| Vertex upsert | **Yes** (only via `UNWIND`) | **No** |
| `read_epoch` / `bookmark` in response | bookmark via driver | **both, in the response body** |

**Ingestion must use Bolt.** The HTTP query engine rejects every vertex-upsert
form with `MERGE with following clauses is not executable in Query engine`, and
a bare `MERGE (n:Label {id: …})` with `only one-hop edge patterns are executable
in Query engine MERGE`. Reads work on either; HTTP is preferred for
judge-visible reads because the response carries `read_epoch` and `bookmark`
directly:

```json
{"query_id":"http-query-1","columns":["id"],"rows":[[{"type":"vertex_id","value":0}]],
 "read_epoch":0,"next_cursor":null,
 "bookmark":"sgk:1:64656661756c74:64656661756c74:63656c6c2d30:0"}
```

The bookmark is `sgk:<version>:<hex namespace>:<hex graph>:<hex cell>:<sequence>`
— `64656661756c74` is `default`, `63656c6c2d30` is `cell-0`, and the trailing
integer is the SlateDB commit sequence. That is a real storage sequence read off
a live response, so the trace can display it without inventing anything.

## A bare `{id: N}` pattern is an address, not an existence check

This is the highest-consequence finding, and it is silent:

```cypher
MATCH (n {id: 999999}) RETURN n.id AS id      -- returns a row. Node never existed.
MATCH (n {id: 999999}) RETURN count(*) AS c   -- returns 1.
MATCH (n:Doc {id: 999999}) RETURN n.id AS id  -- returns nothing. Correct.
```

Node ids are addresses in an object-store-native engine, so an id-only pattern
resolves without hydrating the vertex. Only a label (or another property
predicate) forces hydration and therefore filtering.

**Every existence check carries a label.** Citation validation is the place this
matters most: "does this dsid exist in the graph" written as a bare-id match
passes for ids that were never written, which would make the submission's
100%-valid-citations claim vacuous. `tests/test_hydra_contract.py` pins both
halves.

## Write forms that execute

Vertex upsert — always through `UNWIND`, even for a single row, and the label is
applied in `SET` rather than in the `MERGE` pattern:

```cypher
UNWIND $rows AS row
MERGE (n {id: row.vertex})
SET n:Document, n.dsid = row.dsid, n.title = row.title
```

Folding properties into the pattern (`MERGE (n:Document {id: row.vertex, dsid: …})`)
is rejected: the pattern is the identity being matched, so extra properties
would rewrite what it matched.

Edge upsert — one directed, single-typed relationship per batch, endpoints
matched by label + id first, relationship identified by its own deterministic
`id`:

```cypher
UNWIND $rows AS row
MATCH (s:Entity {id: row.src}), (d:Document {id: row.dst})
MERGE (s)-[r:MENTIONED_IN {id: row.eid}]->(d)
SET r.method = row.method, r.confidence = row.confidence
```

`CREATE` also works here but is not idempotent — replaying it produces a second
parallel edge. `MERGE` keyed on the deterministic edge id is what makes replay
safe. *(cypher-compat.md says an `UNWIND MATCH` "must end in `RETURN` or
`DELETE`"; the engine accepts `MERGE … SET`, and the loader relies on it.)*

A `SET` value inside an `UNWIND` must come off the row map. A literal is
rejected with `UNWIND relationship SET values must read from the row map`, so a
constant is passed as a column rather than written into the statement.

Property values are scalars only — `UNWIND row 0 field tags must be scalar`.
Lists, and therefore aliases, evidence lists, and multi-valued attributes, are
modelled as nodes and edges. `2**63 - 1` round-trips through Bolt exactly, so
63-bit ids are safe.

### Batches are capped at 1024 items

Admission control rejects anything larger:

```
client_query_batch_items rejected by admission control: actual 2000 exceeds limit 1024
```

This is a hard ceiling rather than a tuning knob, so `config.NODE_BATCH_SIZE`
and `config.EDGE_BATCH_SIZE` sit at 1000 and the loader refuses a larger value
up front instead of discovering it mid-ingest.

### A label index holds at most 250,000 vertices

Registering all 511,962 documents under one label fails partway:

```
cypher_vertex_label_index_candidates rejected by admission control:
actual 250001 exceeds limit 250000
```

The failure is worse than a rejected write, because the partial write leaves the
label over its cap and **every unbounded scan of that label then fails too** —
`MATCH (d:Document) RETURN count(*)`, and any `WHERE d.run_id = …` filter that
has to walk the label. Id-anchored and property-anchored lookups keep working
(`MATCH (d:Document {id: $id})`, `MATCH (d:Document {dsid: $dsid})`), so the data
is readable, but so is deletion by label — which means the state cannot be
cleaned up through the label that broke. Recreating the store is the way out.

This is a design constraint rather than a tuning knob, and it points the same
way PLAN.md already does: *Parquet remains the authoritative full-text store; do
not duplicate every body into another large database.* The graph holds the
**enriched working set** — documents that carry mentions, claims, and evidence —
while lexical search covers the whole corpus and has no such ceiling. Retrieval
finds entry points across 512k documents; the graph reasons over the thousands
that have been enriched.

Practical rules that follow:

- Keep any one label comfortably under 250,000. Partition by run or by working
  set, not by "everything of this type".
- Prefer id-anchored reads everywhere. They are the engine's native access path
  and they keep working when a label scan will not.
- Registering ids is separate from writing them: the 525,201 identities for the
  full corpus registered without a single collision, so the id scheme scales
  even where the label index does not.

### Counting a relationship type costs what its anchor label costs

`count(*)` over a relationship pattern is not a lookup on an edge index. The
engine anchors on one endpoint's label and expands every vertex under it, so the
price is set by the anchor rather than by the number of edges returned. Measured
against the same graph:

| pattern | edges | time |
|---|---|---|
| `(:Claim)-[:CONFLICTS_WITH]->(:Claim)` | 23 | 15 ms |
| `(:Entity)-[:PARTICIPATED_IN]->(:Channel)` | 52 | 30 ms |
| `(:Document)-[:ASSERTS]->(:Claim)` | 1,326 | 946 ms |
| `(:Mention)-[:RESOLVES_TO]->(:Entity)` | 4,841 | 3,886 ms |
| `(:Mention)-[:CANDIDATE_FOR]->(:Entity)` | 11,691 | 8,241 ms |

The two slow ones are anchored on `Mention`, of which there were 8,889.
Reversing the pattern to anchor on `Entity` (778 vertices) does **not** help —
8,693 ms, within noise of the original — so the planner is not choosing the
cheaper side.

Dropping the labels to force a relationship scan is worse than slow, it does not
complete:

```
MATCH ()-[e:CANDIDATE_FOR]->() RETURN count(*)
→ cypher_precomputed_cross_join exceeded query timeout after 29999 ms
```

So both endpoints must carry a label — the same rule the existence checks
follow, for a different reason — and a status endpoint should not count
relationships anchored on a label that grows with ingestion. `/api/status`
omits those two by default and takes them behind `?full=1`.

### Measured throughput

5,000 nodes in 0.26 s — **~19,000 rows/second** at a batch size of 1000, steady
across batches. Registering all 511,962 documents is therefore well under a
minute of database time, and Parquet reading and parsing, not the graph, set the
pace of a full ingest. Replaying the same job with a checkpoint present executes
zero batches.

## There is no batch multi-id read

Three separate rejections close off the obvious approaches:

| Attempt | Result |
|---|---|
| `WHERE n.id IN $ids` | `composite parameter $ids is only supported as an UNWIND batch input` |
| `UNWIND $rows AS row MATCH (n:Doc {id: row.id}) RETURN …` | `UNWIND batch supports one-hop relationships only` |
| Two statements in one request | `query transport requires exactly one Cypher statement` |

What works instead, in increasing order of power:

1. **`OR` chain** — `MATCH (n:Doc) WHERE n.id = $a OR n.id = $b RETURN …`.
   Fine for a handful of ids, but it is a label scan, so cost grows with the
   label rather than with the id count. Keep it off the hot path once the
   corpus is loaded.
2. **Id-anchored traversal, one query per anchor** — the workhorse. Each query
   is a cheap address lookup plus a typed adjacency scan:
   `MATCH (e:Entity)-[r:MENTIONED_IN]->(d:Document {id: $id}) RETURN …`.
   Ten candidate documents means ten small queries, which is the intended shape.
3. **`algo.MSpaths`** — the native multi-source fan-out, and the reason the
   graph is not just a store. It resolves many indexed source and target values
   in one call:

```cypher
CALL algo.MSpaths({sourceLabel: 'Entity', sourceProperty: 'name',
                   sourceValues: ['sam', 'soham'],
                   targetLabel: 'Document', targetProperty: 'dsid',
                   targetValues: ['dsid_a', 'dsid_b'],
                   relTypes: ['MENTIONED_IN'], relDirection: 'both',
                   maxLen: 3, pathCount: 5, resultLimit: 50})
YIELD path RETURN path
```

Paths come back as alternating node-property maps and relationship types —
`[{'name': 'sam'}, 'MENTIONED_IN', {'dsid': 'dsid_a', 'title': 'alpha'}]` —
which renders directly as an evidence path without a second hydration query.

Two syntactic constraints on the procedures, both discovered the hard way:

- `sourceLabel` and `targetLabel` must be **string literals**. Passing a
  parameter fails with `sourceLabel must be a string literal`, so the label is
  interpolated and therefore has to be identifier-validated first
  (`hydra_client.check_identifier`).
- Anywhere else in Cypher, **a node carrying a label or a non-id property must
  be named**. `MATCH (:Entity)-[r:MENTIONED_IN]->(:Document)` is rejected with
  `node labels and non-id properties require a named node`; bind the endpoints
  (`MATCH (e:Entity)-[r:MENTIONED_IN]->(d:Document)`) even when the bindings go
  unused.

A bulk `DETACH DELETE` over thousands of nodes also exceeds the transaction
budget (`cypher_delete_ver…`); delete in bounded passes.

### Deleting a relationship inverts the naming rule

Everywhere else, a node carrying a label must be named. Relationship deletion
through `UNWIND` requires the opposite — the endpoints must be **anonymous**:

```cypher
UNWIND $rows AS row MATCH ()-[e:RESOLVES_TO {id: row.eid}]->() DELETE e   ✓
```

Naming them is rejected with `UNWIND relationship property DELETE requires
anonymous endpoints`, and moving the id into a `WHERE` is rejected too — the id
has to sit in the relationship pattern. Both probed directly; the error text
above is the engine's own.

A relationship's `id` is also **not readable**: `RETURN r.id` is rejected with
`unbound variable r`, while `r.method` and every other property on the same
relationship return fine. So an edge cannot be deleted by reading its id back
out of the graph.

It does not need to be. Every edge id is derived deterministically from its type
and its two endpoints, so the id of an edge to remove is recomputed from the
endpoints a `MATCH` does return. That is what makes a resolution decision
revisable at all — `scripts/37_rebuild_resolution.py` deletes superseded
`RESOLVES_TO` edges this way.

## Read clauses confirmed working

`OPTIONAL MATCH` (reads only), `UNION`, `collect()`, `STARTS WITH`, `ORDER BY`
/ `SKIP` / `LIMIT`, bounded variable-length `*1..3`, and `algo.SPpaths` /
`algo.SSpaths` / `algo.MSpaths`.

Not available, and worth remembering before writing a query: `IN`, `CONTAINS`,
`ENDS WITH`, `IS NULL`, `RETURN *`, `min()`, `max()`, `DISTINCT` inside an
aggregate, undirected patterns, unbounded `*`, and `WITH` that aliases or
filters. The maximum on a variable-length pattern is mandatory.
