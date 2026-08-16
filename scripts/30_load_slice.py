#!/usr/bin/env python
"""Build the vertical slice: parse, load structure, then resolve through the graph.

The order matters and is the point of this script. Structure goes in first —
documents, mentions, channels, and the participation those imply — and only then
are ambiguous surfaces resolved, by querying HydraDB over that structure. An
earlier version decided everything in Python and used the graph as a place to
file the answer, which is precisely the arrangement the project exists to argue
against.

Every node and edge is stamped with a run id, so a run can be inspected and torn
down without disturbing another, and every id passes through the collision
registry before it is written.

    uv run python scripts/30_load_slice.py --channel eng-runtime --limit 300
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config  # noqa: E402
from tracegraph.graph_resolve import GraphEvidence  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import IdRegistry, edge_identity, node_identity  # noqa: E402
from tracegraph.loader import Checkpointer, upsert_edges, upsert_nodes  # noqa: E402
from tracegraph.parsers import normalise_content, parse_document  # noqa: E402
from tracegraph.parsers.base import PERSON  # noqa: E402
from tracegraph.resolve import (  # noqa: E402
    METHOD_GRAPH_EVIDENCE,
    METHOD_UNRESOLVED,
    Resolver,
)

DOC = "Document"
ENTITY = "Entity"
MENTION = "Mention"
CHANNEL = "Channel"


class Minter:
    """Mints ids and registers every one before it reaches the graph.

    The registry is not an optional debugging aid: HydraDB resolves a vertex by
    id without consulting its label, so two identities folding onto one 63-bit
    value are the same vertex to the engine. Routing every id through here is
    what makes that a loud failure instead of a silent merge.
    """

    def __init__(self, registry: IdRegistry) -> None:
        self.registry = registry
        self._pending: list = []

    def node(self, node_type: str, natural_key: str) -> int:
        row = node_identity(node_type, natural_key)
        self._pending.append(row)
        return row.id

    def edge(self, edge_type: str, src: int, dst: int, scope: str = "") -> int:
        row = edge_identity(edge_type, src, dst, scope)
        self._pending.append(row)
        return row.id

    def flush(self) -> int:
        """Register everything minted so far. Raises on a collision."""
        if not self._pending:
            return 0
        registered = self.registry.register_many(self._pending)
        self._pending.clear()
        return registered


def fingerprint(rows: list[dict]) -> str:
    """Content hash of a batch, so a checkpoint cannot skip changed input."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(sorted(row.items())).encode())
    return digest.hexdigest()[:16]


def select_slice(channel: str, limit: int, email_limit: int) -> list[dict]:
    """Documents from one Slack channel, plus internal email for identities.

    Slack alone resolves nothing — its speakers are bare handles — so the slice
    deliberately spans both sources.
    """
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    slack: list[dict] = []
    gmail: list[dict] = []

    for batch in parquet.iter_batches(
        batch_size=4000, columns=["doc_id", "source_type", "title", "content"]
    ):
        data = batch.to_pydict()
        if data["source_type"][0] not in ("slack", "gmail"):
            continue
        for i in range(batch.num_rows):
            st = data["source_type"][i]
            row = {
                "doc_id": data["doc_id"][i],
                "source_type": st,
                "title": data["title"][i],
                "content": data["content"][i],
            }
            if st == "slack" and row["title"].strip() == channel and len(slack) < limit:
                slack.append(row)
            elif st == "gmail" and len(gmail) < email_limit:
                gmail.append(row)
        if len(slack) >= limit and len(gmail) >= email_limit:
            break
    return gmail + slack


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", default="eng-runtime")
    ap.add_argument("--limit", type=int, default=300, help="Slack documents")
    ap.add_argument("--email-limit", type=int, default=600, help="Gmail documents")
    ap.add_argument("--run-id", default=None, help="defaults to a timestamp")
    args = ap.parse_args()

    run_id = args.run_id or f"run{int(time.time())}"
    job = f"slice:{run_id}"
    print(f"run {run_id}")

    docs = select_slice(args.channel, args.limit, args.email_limit)
    if not docs:
        print("no documents matched; try another channel", file=sys.stderr)
        return 1
    print(f"  {len(docs)} documents {dict(Counter(d['source_type'] for d in docs))}")

    # --- parse ---------------------------------------------------------------
    parsed, bad_offsets = {}, 0
    for d in docs:
        body = normalise_content(d["content"])
        p = parse_document(d["doc_id"], d["source_type"], d["title"], d["content"])
        verified = p.verified_mentions(body)
        bad_offsets += len(p.mentions) - len(verified)
        p.mentions = verified
        parsed[d["doc_id"]] = p

    total_mentions = sum(len(p.mentions) for p in parsed.values())
    print(f"  parsed {total_mentions} mentions, {bad_offsets} rejected for bad offsets")

    # --- identities and the decisions that need no graph ---------------------
    #
    # An address is an identity and a unique token match is a lookup; neither is
    # a question about structure, so neither goes to the engine.
    resolver = Resolver()
    for d in docs:
        p = parsed[d["doc_id"]]
        resolver.observe(d["doc_id"], d["source_type"], p.mentions,
                         channel=p.attributes.get("channel"))
    merged = resolver.merge_same_person()
    print(f"  {len(resolver.people)} identities from email "
          f"({merged} alternate addresses merged)")

    direct, ambiguous = [], []
    for d in docs:
        p = parsed[d["doc_id"]]
        channel = p.attributes.get("channel")
        for m in p.mentions:
            r = resolver.resolve_mention(m, d["doc_id"], channel, use_graph_tier=False)
            (direct if r.resolved else ambiguous).append((d["doc_id"], m, r, channel))
    print(f"  {len(direct)} resolved without the graph, {len(ambiguous)} need it")

    registry = IdRegistry()
    minter = Minter(registry)
    checkpointer = Checkpointer()

    with HydraClient() as client:
        client.verify()

        # --- phase one: structure -------------------------------------------
        doc_ids = {d["doc_id"]: minter.node(DOC, d["doc_id"]) for d in docs}
        entity_ids = {key: minter.node(ENTITY, key) for key in resolver.people}
        channels = sorted({p.attributes["channel"] for p in parsed.values()
                           if p.attributes.get("channel")})
        channel_ids = {name: minter.node(CHANNEL, name) for name in channels}

        mention_ids: dict[tuple[str, int, int], int] = {}
        for doc_id, p in parsed.items():
            for m in p.mentions:
                mention_ids[(doc_id, m.start, m.end)] = minter.node(
                    MENTION, f"{doc_id}:{m.start}:{m.end}")
        minter.flush()

        def load_nodes(label, rows, props):
            if rows:
                upsert_nodes(client, label, rows, job=f"{job}:{fingerprint(rows)}",
                             properties=props, checkpointer=checkpointer)

        load_nodes(DOC, [
            {"vertex": doc_ids[d["doc_id"]], "dsid": d["doc_id"],
             "source_type": d["source_type"], "title": d["title"][:500],
             "run_id": run_id}
            for d in docs
        ], ["dsid", "source_type", "title", "run_id"])

        load_nodes(ENTITY, [
            {"vertex": entity_ids[key], "key": key, "name": person.display_name,
             "kind": PERSON, "emails": ";".join(sorted(person.emails))[:400],
             "domains": ";".join(sorted(person.domains))[:200], "run_id": run_id}
            for key, person in resolver.people.items()
        ], ["key", "name", "kind", "emails", "domains", "run_id"])

        load_nodes(CHANNEL, [
            {"vertex": cid, "name": name, "run_id": run_id}
            for name, cid in channel_ids.items()
        ], ["name", "run_id"])

        # Mentions carry their own resolution status, so an unresolved surface
        # is a recorded decision rather than a missing edge. Absence of an edge
        # cannot otherwise be told apart from a failed write.
        load_nodes(MENTION, [
            {"vertex": mention_ids[(doc_id, m.start, m.end)],
             "surface": m.surface[:300], "normalised": m.surface.casefold()[:300],
             "kind": m.kind, "role": m.role, "start": m.start, "end": m.end,
             "dsid": doc_id, "run_id": run_id,
             "status": "pending", "method": "", "candidates": 0, "reason": ""}
            for doc_id, p in parsed.items() for m in p.mentions
        ], ["surface", "normalised", "kind", "role", "start", "end", "dsid",
            "run_id", "status", "method", "candidates", "reason"])

        mentioned_in = [
            {"src": mention_ids[(doc_id, m.start, m.end)], "dst": doc_ids[doc_id],
             "eid": minter.edge("MENTIONED_IN",
                                mention_ids[(doc_id, m.start, m.end)],
                                doc_ids[doc_id]),
             "role": m.role, "run_id": run_id}
            for doc_id, p in parsed.items() for m in p.mentions
        ]
        upsert_edges(client, "MENTIONED_IN", mentioned_in,
                     job=f"{job}:{fingerprint(mentioned_in)}",
                     source_label=MENTION, target_label=DOC,
                     properties=["role", "run_id"], checkpointer=checkpointer)

        sent_in = [
            {"src": doc_ids[doc_id], "dst": channel_ids[p.attributes["channel"]],
             "eid": minter.edge("SENT_IN", doc_ids[doc_id],
                                channel_ids[p.attributes["channel"]]),
             "run_id": run_id}
            for doc_id, p in parsed.items() if p.attributes.get("channel")
        ]
        if sent_in:
            upsert_edges(client, "SENT_IN", sent_in,
                         job=f"{job}:{fingerprint(sent_in)}", source_label=DOC,
                         target_label=CHANNEL, properties=["run_id"],
                         checkpointer=checkpointer)

        # Direct resolutions, and the participation they imply. Participation is
        # what the graph tier reads, so it has to exist before scoring runs.
        resolves, participation = [], {}
        for doc_id, m, r, channel in direct:
            mid = mention_ids[(doc_id, m.start, m.end)]
            target = entity_ids[r.person_key]
            resolves.append({
                "src": mid, "dst": target,
                "eid": minter.edge("RESOLVES_TO", mid, target),
                "method": r.method, "confidence": r.confidence,
                "evidence": r.evidence[:400], "candidates": len(r.candidates),
                "run_id": run_id,
            })
            if channel:
                participation.setdefault((r.person_key, channel), {
                    "src": target, "dst": channel_ids[channel],
                    "eid": minter.edge("PARTICIPATED_IN", target,
                                       channel_ids[channel]),
                    "run_id": run_id,
                })
        minter.flush()

        if resolves:
            upsert_edges(client, "RESOLVES_TO", resolves,
                         job=f"{job}:direct:{fingerprint(resolves)}",
                         source_label=MENTION, target_label=ENTITY,
                         properties=["method", "confidence", "evidence",
                                     "candidates", "run_id"],
                         checkpointer=checkpointer)
        if participation:
            rows = list(participation.values())
            upsert_edges(client, "PARTICIPATED_IN", rows,
                         job=f"{job}:{fingerprint(rows)}", source_label=ENTITY,
                         target_label=CHANNEL, properties=["run_id"],
                         checkpointer=checkpointer)
        print(f"  structure loaded: {len(participation)} participation edges")

        # --- phase two: the graph decides ------------------------------------
        evidence = GraphEvidence(client, run_id)
        graph_resolves, candidate_edges, unresolved = [], [], []
        decided = 0

        for doc_id, m, r, channel in ambiguous:
            mid = mention_ids[(doc_id, m.start, m.end)]
            candidates = {
                entity_ids[key]: resolver.people[key].display_name
                for key in r.candidates if key in entity_ids
            }
            if not candidates:
                unresolved.append((mid, METHOD_UNRESOLVED, 0, r.evidence))
                continue

            decision = evidence.score_candidates(
                candidates, doc_ids[doc_id],
                channel_ids.get(channel) if channel else None,
            )

            # Every candidate considered is recorded, so a rejected one can be
            # inspected rather than inferred from its absence.
            for entry in decision.scored[:8]:
                candidate_edges.append({
                    "src": mid, "dst": entry.entity_id,
                    "eid": minter.edge("CANDIDATE_FOR", mid, entry.entity_id),
                    "score": entry.score, "co_occurrences": entry.co_occurrences,
                    "participations": entry.participations, "run_id": run_id,
                })

            if decision.winner is None:
                unresolved.append(
                    (mid, METHOD_UNRESOLVED, len(candidates), decision.reason))
                continue

            decided += 1
            graph_resolves.append({
                "src": mid, "dst": decision.winner.entity_id,
                "eid": minter.edge("RESOLVES_TO", mid, decision.winner.entity_id),
                "method": METHOD_GRAPH_EVIDENCE,
                "confidence": round(0.5 + 0.45 * decision.margin, 3),
                "evidence": decision.reason[:400], "candidates": len(candidates),
                "run_id": run_id,
            })
        minter.flush()

        if graph_resolves:
            upsert_edges(client, "RESOLVES_TO", graph_resolves,
                         job=f"{job}:graph:{fingerprint(graph_resolves)}",
                         source_label=MENTION, target_label=ENTITY,
                         properties=["method", "confidence", "evidence",
                                     "candidates", "run_id"],
                         checkpointer=checkpointer)
        if candidate_edges:
            upsert_edges(client, "CANDIDATE_FOR", candidate_edges,
                         job=f"{job}:{fingerprint(candidate_edges)}",
                         source_label=MENTION, target_label=ENTITY,
                         properties=["score", "co_occurrences", "participations",
                                     "run_id"],
                         checkpointer=checkpointer)

        # Statuses last, so every mention carries its own outcome.
        status_rows = [
            {"vertex": mention_ids[(doc_id, m.start, m.end)], "status": "resolved",
             "method": r.method, "candidates": len(r.candidates),
             "reason": r.evidence[:300]}
            for doc_id, m, r, _ in direct
        ] + [
            {"vertex": row["src"], "status": "resolved",
             "method": METHOD_GRAPH_EVIDENCE, "candidates": row["candidates"],
             "reason": row["evidence"][:300]}
            for row in graph_resolves
        ] + [
            {"vertex": mid, "status": "unresolved", "method": method,
             "candidates": count, "reason": (reason or "")[:300]}
            for mid, method, count, reason in unresolved
        ]
        load_nodes(MENTION, status_rows,
                   ["status", "method", "candidates", "reason"])

        print(f"  graph decided {decided}/{len(ambiguous)} ambiguous surfaces "
              f"in {evidence.queries} queries")
        print(f"  registry: {registry.count()} identities "
              f"({registry.count('node')} nodes, {registry.count('edge')} edges)")

        print("\ngraph for this run")
        for label in (DOC, ENTITY, MENTION, CHANNEL):
            rows = client.bolt_read(
                f"MATCH (n:{label}) WHERE n.run_id = $r RETURN count(*) AS c",
                {"r": run_id})
            print(f"  {label:9} {rows[0]['c']:>6}")

    checkpointer.close()
    print(f"\nrun id: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
