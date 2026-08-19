# Upstream issues to file at github.com/hydra-db/hydradb

Two findings from `docs/engine-notes.md` that are reproducible bugs rather than
project-specific notes, written up so they can be pasted straight into the
tracker. Both were found by probing the pinned image during Hack Hydra 2026, and
both are pinned by `tests/test_hydra_contract.py` in this repository.

File the second one first if only one goes in — it can make data unrecoverable.

---

## Issue 1 — Label index overflow makes the label unscannable, including by the delete that would repair it

**Title:** `Label index overflow is unrecoverable: the failed write leaves the label unscannable, including by DELETE`

**Labels:** bug, data-loss

**Body:**

Registering more than 250,000 vertices under one label fails partway, which is
expected and documented by the error. What is not expected is the state it
leaves behind: the partial write leaves the label over its cap, and from then on
**every unbounded scan of that label fails — including the delete that would
undo it.**

Image: `ghcr.io/hydra-db/hydradb@sha256:db78309a233be54662db29744047e985a39b51c45a270d1a1f47c31a62cdb709`

### Reproducing

Write vertices under a single label past the admission limit:

```
cypher_vertex_label_index_candidates rejected by admission control:
actual 250001 exceeds limit 250000
```

Then, against the same store:

```cypher
MATCH (d:Document) RETURN count(*) AS c        -- fails
MATCH (d:Document) WHERE d.run_id = $r RETURN d -- fails (must walk the label)
MATCH (d:Document) DELETE d                     -- fails: cannot clean up
```

Id- and property-anchored reads keep working, so the data is still reachable:

```cypher
MATCH (d:Document {id: $id})     RETURN d   -- works
MATCH (d:Document {dsid: $dsid}) RETURN d   -- works
```

### Why this is worse than a rejected write

A rejected write is recoverable. This is not: the only operation that could
return the label to a legal size is itself blocked by the label being over that
size. In our case the only remaining option was to recreate the store and
re-ingest, which for a 500k-document corpus is hours.

### Suggested fixes, in order of preference

1. **Make the admission check pre-emptive** — reject the batch that *would*
   cross the cap, before any of it is applied, leaving the label at its last
   legal size.
2. **Exempt `DELETE` and `count(*)` from the label-scan path** when the label is
   over cap, so the state remains repairable from Cypher.
3. Failing both, **document the ceiling prominently** and surface remaining
   headroom, so callers can partition before they hit it rather than after.

### Impact

Any workload that partitions by type rather than by working set will hit this,
because "all documents" or "all users" is the obvious first label to reach for.
The failure arrives with no warning at 250,000 and cannot be walked back.

---

## Issue 2 — A bare `{id: N}` pattern matches ids that were never written

**Title:** `MATCH (n {id: N}) returns a row for ids that do not exist — existence checks written this way are vacuously true`

**Labels:** bug, docs

**Body:**

An id-only pattern resolves an address without hydrating the vertex, so it
returns a row whether or not anything was ever written there. Adding a label —
or any other property predicate — forces hydration and the match filters
correctly.

Image: `ghcr.io/hydra-db/hydradb@sha256:db78309a233be54662db29744047e985a39b51c45a270d1a1f47c31a62cdb709`

### Reproducing

Against an empty store, with `999999` never written:

```cypher
MATCH (n {id: 999999}) RETURN n.id AS id       -- returns a row
MATCH (n {id: 999999}) RETURN count(*) AS c    -- returns 1
MATCH (n:Doc {id: 999999}) RETURN n.id AS id   -- returns nothing  ← correct
```

### Why it matters

This is silent, and it breaks the most natural way to write an existence check.
In our project the check is *"does this document id actually exist in the graph
before we cite it to a user"*. Written as a bare-id match it passes for every id,
including ones never written — so a validation step that looks correct, has a
test, and appears to pass is in fact asserting nothing at all.

We only found it because we probed the negative case deliberately. Anyone
reasonably assuming Neo4j-compatible semantics here — where `MATCH (n {id: N})`
filters — would ship the bug.

### Suggested fixes

1. **Document it prominently** in the Cypher compatibility guide. This is a real
   semantic divergence from Neo4j, and it belongs beside the other differences
   rather than being discovered.
2. If the divergence is not intentional, make an id-only pattern hydrate and
   filter like any other property predicate.
3. Consider a warning when a query's only predicate is `id`, since the pattern is
   almost always meant as a lookup of something that exists.

### Note

This is documented and pinned in our submission — `docs/engine-notes.md` and
`tests/test_hydra_contract.py` — so the contract test fails loudly if an engine
upgrade changes the behaviour in either direction.
