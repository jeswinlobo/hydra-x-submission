"""Candidate scoring performed by HydraDB.

The first version of this scored candidates by intersecting Python sets and then
stored the winner, which made the graph a place to put answers rather than the
thing that produced them. That is the opposite of what this project claims, and
it would have made the planned baseline-versus-graph ablation measure nothing:
with the decision already made in Python, a run with the graph and a run without
it return identical results.

Here the evidence counts come out of the engine. Structure is written first —
who participates in which channel, which mention sits in which document — and
then every ambiguous surface is scored by traversals over that structure:

* **Co-occurrence** (two hops): does this candidate already appear in the very
  document where the ambiguous handle spoke?
      Entity <-[:RESOLVES_TO]- Mention -[:MENTIONED_IN]-> Document
* **Participation** (one hop): does this candidate participate in the channel
  the handle spoke in?
      Entity -[:PARTICIPATED_IN]-> Channel

Both are Cypher aggregates. Remove HydraDB and there is nothing left to score
with, which is the property the submission has to be able to demonstrate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hydra_client import HydraClient

# Co-occurrence in the same document is much stronger evidence than sharing a
# channel: a channel can hold hundreds of people, while appearing in the same
# short document alongside the handle is close to direct. The weights are
# reported alongside every decision so a reader can disagree with them.
WEIGHT_CO_OCCURRENCE = 3
WEIGHT_PARTICIPATION = 1


@dataclass
class CandidateScore:
    entity_id: int
    entity_name: str
    co_occurrences: int = 0
    participations: int = 0

    @property
    def score(self) -> int:
        return (
            self.co_occurrences * WEIGHT_CO_OCCURRENCE
            + self.participations * WEIGHT_PARTICIPATION
        )

    def describe(self) -> str:
        parts = []
        if self.co_occurrences:
            parts.append(f"{self.co_occurrences} co-mention(s) in the same document")
        if self.participations:
            parts.append(f"{self.participations} shared channel(s)")
        return " and ".join(parts) if parts else "no graph evidence"


@dataclass
class GraphDecision:
    """The outcome of scoring one ambiguous surface against the graph."""

    winner: CandidateScore | None
    runner_up: CandidateScore | None
    scored: list[CandidateScore] = field(default_factory=list)
    reason: str = ""

    @property
    def margin(self) -> float:
        if self.winner is None or self.winner.score == 0:
            return 0.0
        runner = self.runner_up.score if self.runner_up else 0
        return (self.winner.score - runner) / self.winner.score


class GraphEvidence:
    """Scores resolution candidates with queries against HydraDB.

    Results are cached per (candidate, document) and (candidate, channel) pair
    because a handle repeats many times in one channel and the same question
    would otherwise be asked of the engine thousands of times.
    """

    def __init__(self, client: HydraClient, run_id: str) -> None:
        self.client = client
        self.run_id = run_id
        self._co_cache: dict[tuple[int, int], int] = {}
        self._part_cache: dict[tuple[int, int], int] = {}
        self.queries = 0

    def co_occurrence(self, entity_id: int, document_id: int) -> int:
        """How often this entity is already resolved inside this document.

        Two hops, anchored on both ends by id, so the engine walks a typed
        adjacency rather than scanning a label.
        """
        key = (entity_id, document_id)
        if key not in self._co_cache:
            self.queries += 1
            rows = self.client.bolt_read(
                "MATCH (e:Entity {id: $eid})<-[:RESOLVES_TO]-(m:Mention)"
                "-[:MENTIONED_IN]->(d:Document {id: $did}) "
                "RETURN count(*) AS n",
                {"eid": int(entity_id), "did": int(document_id)},
            )
            self._co_cache[key] = int(rows[0]["n"]) if rows else 0
        return self._co_cache[key]

    def participation(self, entity_id: int, channel_id: int) -> int:
        """Whether this entity participates in this channel, per the graph."""
        key = (entity_id, channel_id)
        if key not in self._part_cache:
            self.queries += 1
            rows = self.client.bolt_read(
                "MATCH (e:Entity {id: $eid})-[:PARTICIPATED_IN]->(c:Channel {id: $cid}) "
                "RETURN count(*) AS n",
                {"eid": int(entity_id), "cid": int(channel_id)},
            )
            self._part_cache[key] = int(rows[0]["n"]) if rows else 0
        return self._part_cache[key]

    def score_candidates(
        self,
        candidates: dict[int, str],
        document_id: int,
        channel_id: int | None,
    ) -> GraphDecision:
        """Rank candidates for one ambiguous surface.

        A tie leaves the surface unresolved. Breaking it on frequency, or on
        whichever candidate the dictionary happened to yield first, would
        manufacture a decision the evidence does not support, and an unresolved
        mention is a supportable answer where a wrong merge is not.
        """
        scored: list[CandidateScore] = []
        for entity_id, name in candidates.items():
            entry = CandidateScore(entity_id=entity_id, entity_name=name)
            entry.co_occurrences = self.co_occurrence(entity_id, document_id)
            if channel_id is not None:
                entry.participations = self.participation(entity_id, channel_id)
            scored.append(entry)

        scored.sort(key=lambda c: (-c.score, c.entity_name))
        if not scored or scored[0].score == 0:
            return GraphDecision(
                None, None, scored,
                reason=f"none of {len(scored)} candidates is connected to this "
                       "document or channel in the graph",
            )

        winner = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None
        if runner_up is not None and runner_up.score == winner.score:
            return GraphDecision(
                None, runner_up, scored,
                reason=(
                    f"{winner.entity_name} and {runner_up.entity_name} are tied on "
                    f"{winner.score} points of graph evidence"
                ),
            )

        return GraphDecision(
            winner, runner_up, scored,
            reason=(
                f"{winner.entity_name} has {winner.describe()}, against "
                f"{runner_up.describe() if runner_up else 'nothing'} for the "
                f"next of {len(scored)} candidates"
            ),
        )

    def propose_from_structure(
        self, initial: str, document_id: int, channel_id: int | None,
    ) -> list[tuple[str, int, int, int]]:
        """Candidates the graph can see that no string rule would have offered.

        This is the case the track brief leads with — *"deciding that 'Sam',
        '@soham' and 'S. Ratnaparkhi' are one person"* — and the first of the
        three is the one string matching provably cannot reach. `sam` shares no
        token with `soham ratnaparkhi`, so edit distance, token subsets and
        embeddings all return nothing: there is no lexical signal to find,
        because the two strings genuinely have almost nothing in common.

        What *does* connect them is structure. Somebody called `sam` speaks in a
        channel; Soham Ratnaparkhi participates in that channel; Soham is already
        resolved elsewhere in this very document. None of that is in the string.

        So instead of scoring candidates a string rule proposed, this asks the
        graph to propose them: every person who participates in this channel or
        is already resolved inside this document, with their evidence counts.
        Returns `(entity_key, name, co_occurrence, participation)` per candidate.

        **The initial is a guard, not the signal.** Requiring a shared first
        letter costs almost nothing against real short forms — `sam`/`Soham`,
        `liz`/`Elizabeth`, `dev`/`Devon` — and it stops the graph from proposing
        that `sam` means `Priya Nair` merely because Priya happens to be the only
        other person in a quiet channel. Structure decides which candidate wins;
        the initial decides which are eligible to compete. Without it this tier
        would be "one person is nearby, so the handle must be them", which is the
        false merge the whole resolution module exists to refuse.
        """
        rows = self.client.bolt_read(
            # Two sources of candidates in one read: people already resolved in
            # this document, and people who participate in its channel.
            #
            # The initial is *not* filtered here, and that is a correction rather
            # than an oversight. `STARTS WITH` is case-sensitive in this engine,
            # and `Entity.name` is the first surface ever seen for a person —
            # which in a mail corpus is usually a bare address, so 21% of
            # entities are named `sergio.alvarez@redwood.ai` rather than
            # `Sergio Alvarez`. Filtering on `'S'` in Cypher hid 42 of 217
            # same-initial people, and hiding a *true* candidate is far worse
            # than returning extra ones: it leaves a wrong candidate as the sole
            # scorer, which is exactly how the caller's guard concludes it has an
            # unambiguous answer. The filter now runs caller-side, case-folded.
            "MATCH (e:Entity)<-[:RESOLVES_TO]-(m:Mention)-[:MENTIONED_IN]->"
            "(d:Document {id: $did}) "
            "WHERE e.run_id = $r "
            "RETURN DISTINCT e.id AS eid, e.key AS key, e.name AS name",
            {"did": int(document_id), "r": self.run_id},
        )
        seen = {r["eid"]: r for r in rows}

        if channel_id is not None:
            for row in self.client.bolt_read(
                "MATCH (e:Entity)-[:PARTICIPATED_IN]->(c:Channel {id: $cid}) "
                "WHERE e.run_id = $r "
                "RETURN DISTINCT e.id AS eid, e.key AS key, e.name AS name",
                {"cid": int(channel_id), "r": self.run_id},
            ):
                seen.setdefault(row["eid"], row)

        self.queries += 1 + (1 if channel_id is not None else 0)

        # Every row here already co-occurs or participates by construction — the
        # first query matched inside this document, the second inside this
        # channel — so re-reading those counts cannot exclude anybody. They are
        # fetched only to report *how much* evidence each candidate has, and the
        # entity id is carried through rather than re-derived from the key.
        proposed: list[tuple[str, str, int, int, int]] = []
        folded = initial.casefold()
        for eid, row in seen.items():
            name = row["name"] or ""
            if not name.casefold().startswith(folded):
                continue
            co = self.co_occurrence(eid, document_id)
            part = self.participation(eid, channel_id) if channel_id is not None else 0
            proposed.append((row["key"], name, co, part, eid))
        return proposed

    def evidence_path(self, entity_id: int, channel_id: int) -> list | None:
        """A renderable path behind a decision, straight from the engine.

        The UI shows this rather than a sentence the application composed, so
        the explanation and the data cannot drift apart.
        """
        self.queries += 1
        rows = self.client.bolt_read(
            "CALL algo.SPpaths({sourceNode: $src, targetNode: $dst, "
            "relTypes: ['PARTICIPATED_IN'], maxLen: 2, relDirection: 'both', "
            "pathCount: 1}) YIELD path RETURN path",
            {"src": int(entity_id), "dst": int(channel_id)},
        )
        return rows[0]["path"] if rows else None
