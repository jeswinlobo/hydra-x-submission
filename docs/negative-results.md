# Negative results

Things that were built, measured, and removed — and the measurements that
decided it. They live here rather than in the README because a reader deciding
whether to trust the system needs the results first and the graveyard second,
not the other way round. Nothing here is softened; several of these are the most
useful findings the project produced.

The short version, and the reason it is worth reading: two graph-retrieval
features worked as queries and neither survived measurement, one because it
changed no answers and one because the run-to-run noise floor of the system
turned out to be larger than the effect being measured. Establishing *that*
— that a single-run A/B on this system cannot distinguish a retrieval feature
from model nondeterminism — is a more useful result than either feature would
have been.

---

**Two attempts to make the graph do retrieval work were measured and removed.**

The second was the promising one: resolve a person the *question* names — not a
document search happened to return — and walk
`Entity ←RESOLVES_TO— Mention —MENTIONED_IN→ Document —ASSERTS→ Claim`. That
starts somewhere lexical search cannot: `sam` is not a word that retrieves Sam
Tyler's documents, it retrieves every document containing the string, whereas
the resolver has already decided which of nineteen people the surface means. The
traversal works — it resolves `Grace O'Connor` and `@soham` to single identities
and returns their documents in about 400ms.

It was removed because the measurement could not support it, and the reason is
worth more than the feature. Over twenty alias-heavy questions, a graph-found
document was cited in **2**. But of the twelve questions where the graph seeded
*nothing at all* — where the two variants are the same code path — **four
changed verdict anyway**, purely from model nondeterminism. The noise floor was
larger than the signal, so a single-run A/B on this system cannot distinguish a
retrieval feature from run-to-run variation. Establishing that would need
repeated trials the deadline does not allow, and shipping an unproven feature
that *looks* like graph reasoning is worse than shipping without it.

The attempt did surface one thing worth stating, and it came out the opposite
way to the intuition. Synthesis sees `evidence[:40]` against roughly 140 claims
from eight retrieved documents, which looks like waste. Raising it to 96 was
measured and reverted: ten rounds produced a crash, a wrong verdict, and
synthesis times of 105s, 58s and 57s against a p50 of 8s. More evidence made the
answer slower and less stable rather than better. The bound is doing work, and
"the model only sees a third of it" was the wrong way to read it.

**An earlier, blunter version was removed for a clearer reason.** The obvious
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

Where the graph *does* do multi-hop work is identity: 1,066 mention occurrences
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
