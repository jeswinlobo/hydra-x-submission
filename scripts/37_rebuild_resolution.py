#!/usr/bin/env python
"""Re-decide every identity in the graph, and remove the decisions that were wrong.

Entity resolution is the part of this track that is actually hard, and a
resolution rule that improves is worth nothing if the graph keeps the answers
the old rule gave. This re-runs the decision over every document the graph
holds and reconciles: an edge that no longer reflects the rule is deleted, the
right one is written, and every mention ends with a status it earned.

What changed under it. Merging used to fold together anyone sharing a full
name, so Elena Rossi at cardiotech.com absorbed Elena Rossi at microsoft.com,
and 76 identities spanning genuinely different organisations collected 366
mentions between them. Merging now also requires an organisational root in
common, which keeps Grace O'Connor's fourteen Redwood spellings together and
leaves the four Elena Rossis apart.

No model calls. Parsing and resolution are deterministic, so this is minutes of
CPU and no spend.

    uv run python scripts/37_rebuild_resolution.py            # report only
    uv run python scripts/37_rebuild_resolution.py --apply
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph import config  # noqa: E402
from tracegraph.graph_resolve import GraphEvidence  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import IdRegistry, edge_identity, node_identity  # noqa: E402
from tracegraph.loader import upsert_edges, upsert_nodes  # noqa: E402
from tracegraph.parquet_reader import RowLocator  # noqa: E402
from tracegraph.parsers import normalise_content, parse_document  # noqa: E402
from tracegraph.parsers.base import PERSON  # noqa: E402
from tracegraph.resolve import (  # noqa: E402
    METHOD_GRAPH_EVIDENCE,
    METHOD_UNRESOLVED,
    Resolver,
    pack,
)

DOC, ENTITY, MENTION, CHANNEL = "Document", "Entity", "Mention", "Channel"

# Deletes run in bounded passes: a bulk delete exceeds the engine's transaction
# budget. See docs/engine-notes.md.
DELETE_BATCH = 400


def delete_edges(client: HydraClient, rel: str, eids: list[int]) -> int:
    """Remove relationships by id.

    The endpoints must be anonymous here, which inverts the rule everywhere
    else in this codebase — see docs/engine-notes.md.
    """
    for start in range(0, len(eids), DELETE_BATCH):
        chunk = eids[start : start + DELETE_BATCH]
        client.bolt_write(
            f"UNWIND $rows AS row MATCH ()-[e:{rel} {{id: row.eid}}]->() DELETE e",
            {"rows": [{"eid": int(e)} for e in chunk]})
    return len(eids)


def load_and_parse(client: HydraClient, run_id: str) -> dict:
    """Every document the graph holds, re-read from the corpus and re-parsed."""
    rows = client.bolt_read(
        "MATCH (d:Document) WHERE d.run_id = $r "
        "RETURN d.dsid AS dsid, d.source_type AS source_type, d.title AS title",
        {"r": run_id})
    locator = RowLocator(config.locator_parquet(), config.locator_db(),
                         require_complete=True)
    parsed = {}
    try:
        for row in rows:
            record = locator.fetch(row["dsid"])
            if record is None:
                continue
            raw = record.get("content") or ""
            body = normalise_content(raw)
            doc = parse_document(row["dsid"], record.get("source_type") or "",
                                 record.get("title") or "", raw)
            parsed[row["dsid"]] = {
                "parsed": doc,
                "mentions": doc.verified_mentions(body)[:400],
                "channel": doc.attributes.get("channel"),
            }
    finally:
        locator.close()
    return parsed


def repoint_merged_away(client: HydraClient, run_id: str, resolver: Resolver,
                        entity_ids: dict, want: dict) -> int:
    """Move mentions off identities the merge folded into somebody else.

    The pass above reconciles every mention it re-derives from the corpus. A
    mention the graph holds but re-parsing no longer produces — offsets shifted,
    or it fell past the per-document cap — is never revisited, so it keeps
    pointing at whichever vertex it was given first. When that vertex has since
    been merged away, the result is a duplicate identity: the same person
    reachable under two vertices, which is the fragmentation this whole module
    exists to prevent, arriving by the back door.

    Both vertices stay — deleting one would orphan whatever else references it —
    but every mention is moved onto the survivor, so nothing resolves to a
    identity the rule no longer recognises.
    """
    live = set(entity_ids)
    rows = client.bolt_read(
        "MATCH (e:Entity) WHERE e.run_id = $run RETURN e.id AS id, e.key AS key",
        {"run": run_id})
    moved, deletions, additions = 0, [], []
    for row in rows:
        key = row["key"]
        if not key or key in live:
            continue
        address = key.split(":", 1)[-1]
        survivor = resolver._by_email.get(address)
        if not survivor or survivor not in entity_ids:
            continue
        target = entity_ids[survivor].id
        for m in client.bolt_read(
                "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) "
                "WHERE e.id = $eid AND r.run_id = $run RETURN m.id AS mid",
                {"eid": row["id"], "run": run_id}):
            mid = m["mid"]
            if want.get(mid) == row["id"]:
                continue  # the rebuild genuinely wants it here
            deletions.append(edge_identity("RESOLVES_TO", mid, row["id"]).id)
            additions.append({
                "src": mid, "dst": target,
                "eid": edge_identity("RESOLVES_TO", mid, target).id,
                "method": "merged_identity", "confidence": 1.0,
                "evidence": f"{address} was folded into {survivor}",
                "candidates": 1, "run_id": run_id})
            moved += 1

    if deletions:
        delete_edges(client, "RESOLVES_TO", deletions)
    if additions:
        upsert_edges(client, "RESOLVES_TO", additions,
                     job=f"rebuild-repoint:{run_id}", source_label=MENTION,
                     target_label=ENTITY,
                     properties=["method", "confidence", "evidence",
                                 "candidates", "run_id"])
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the reconciliation")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    started = time.perf_counter()
    with HydraClient() as client:
        client.verify()
        run_id = args.run_id
        if not run_id:
            rows = client.bolt_read(
                "MATCH (d:Document) RETURN d.run_id AS r ORDER BY r DESC LIMIT 1")
            if not rows:
                print("no ingested run", file=sys.stderr)
                return 1
            run_id = rows[0]["r"]
        print(f"run {run_id}")

        # --- re-decide, from the documents up --------------------------------
        parsed = load_and_parse(client, run_id)
        print(f"  re-parsed {len(parsed)} documents "
              f"({sum(len(p['mentions']) for p in parsed.values())} mentions)")

        resolver = Resolver()
        for dsid, entry in parsed.items():
            resolver.observe(dsid, "", entry["mentions"], channel=entry["channel"])
        merged = resolver.merge_same_person()
        print(f"  {len(resolver.people)} identities ({merged} alternate addresses "
              "folded, same organisation only)")

        registry = IdRegistry()
        entity_ids = {key: node_identity(ENTITY, key) for key in resolver.people}
        channels = sorted({p["channel"] for p in parsed.values() if p["channel"]})
        channel_ids = {name: node_identity(CHANNEL, name) for name in channels}

        # --- what the graph currently believes -------------------------------
        # The endpoints, not the edge id — the engine will not return `r.id` for
        # a relationship (see docs/engine-notes.md). It does not need to: every
        # edge id is derived from its type and endpoints, so an edge that has to
        # be deleted can be addressed by recomputing what it must have been.
        existing = defaultdict(list)
        for row in client.bolt_read(
                "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $run "
                "RETURN m.id AS mid, e.id AS eid LIMIT 20000",
                {"run": run_id}):
            existing[row["mid"]].append(
                (row["eid"], edge_identity("RESOLVES_TO", row["mid"], row["eid"]).id))

        # --- the decision, mention by mention --------------------------------
        evidence = GraphEvidence(client, run_id)
        want: dict[int, int] = {}
        statuses, resolves, participation = [], [], {}
        ambiguous = []

        for dsid, entry in parsed.items():
            channel = entry["channel"]
            for mention in entry["mentions"]:
                mid = node_identity(MENTION, f"{dsid}:{mention.start}:{mention.end}").id
                outcome = resolver.resolve_mention(mention, dsid, channel,
                                                   use_graph_tier=False)
                if outcome.resolved and outcome.person_key in entity_ids:
                    target = entity_ids[outcome.person_key].id
                    want[mid] = target
                    statuses.append({
                        "vertex": mid, "status": "resolved", "method": outcome.method,
                        "candidates": len(outcome.candidates),
                        "reason": outcome.evidence[:300]})
                    resolves.append({
                        "src": mid, "dst": target,
                        "eid": edge_identity("RESOLVES_TO", mid, target).id,
                        "method": outcome.method, "confidence": outcome.confidence,
                        "evidence": outcome.evidence[:400],
                        "candidates": len(outcome.candidates), "run_id": run_id})
                    if channel:
                        key = (outcome.person_key, channel)
                        participation.setdefault(key, {
                            "src": target, "dst": channel_ids[channel].id,
                            "eid": edge_identity("PARTICIPATED_IN", target,
                                                 channel_ids[channel].id).id,
                            "run_id": run_id})
                else:
                    ambiguous.append((dsid, mention, outcome, channel, mid))

        print(f"  {len(want)} decided without the graph, {len(ambiguous)} need it")

        if not args.apply:
            # Reporting stops here. Scoring the ambiguous surfaces needs the
            # participation edges this run would write first, so a dry run
            # cannot preview those decisions honestly and does not pretend to.
            provisional = [(mid, eid) for mid, pairs in existing.items()
                           for eid, _ in pairs
                           if mid in want and want[mid] != eid]
            print(f"  {len(provisional)} directly-decided edges already disagree "
                  "with the new rule")
            for mid, eid in provisional[:6]:
                row = client.bolt_read(
                    "MATCH (e:Entity) WHERE e.id = $e RETURN e.key AS k", {"e": eid})
                right = client.bolt_read(
                    "MATCH (e:Entity) WHERE e.id = $e RETURN e.key AS k",
                    {"e": want[mid]})
                if row:
                    print(f"      {row[0]['k']}  →  "
                          f"{right[0]['k'] if right else '(new identity)'}")
            print("\nre-run with --apply to reconcile")
            return 0

        # --- write ------------------------------------------------------------
        pending = list(entity_ids.values()) + list(channel_ids.values())
        upsert_nodes(client, ENTITY, [
            {"vertex": entity_ids[key].id, "key": key, "name": person.display_name,
             "kind": PERSON, "emails": pack(sorted(person.emails), 400),
             "domains": pack(sorted(person.domains), 200), "run_id": run_id}
            for key, person in resolver.people.items()],
            job=f"rebuild-e:{run_id}",
            properties=["key", "name", "kind", "emails", "domains", "run_id"])
        if channel_ids:
            upsert_nodes(client, CHANNEL, [
                {"vertex": cid.id, "name": name, "run_id": run_id}
                for name, cid in channel_ids.items()],
                job=f"rebuild-ch:{run_id}", properties=["name", "run_id"])

        # Participation first: it is what the graph tier reads, so it has to
        # exist before scoring runs.
        if participation:
            upsert_edges(client, "PARTICIPATED_IN", list(participation.values()),
                         job=f"rebuild-p:{run_id}", source_label=ENTITY,
                         target_label=CHANNEL, properties=["run_id"])

        # The graph tier, for surfaces the tiers above could not separate.
        graph_resolves, candidate_edges = [], []
        for dsid, mention, outcome, channel, mid in ambiguous:
            candidates = {entity_ids[k].id: resolver.people[k].display_name
                          for k in outcome.candidates if k in entity_ids}
            if not candidates:
                statuses.append({"vertex": mid, "status": "unresolved",
                                 "method": METHOD_UNRESOLVED, "candidates": 0,
                                 "reason": (outcome.evidence or "no candidate")[:300]})
                continue
            decision = evidence.score_candidates(
                candidates, node_identity(DOC, dsid).id,
                channel_ids[channel].id if channel else None)
            for entry in decision.scored[:8]:
                candidate_edges.append({
                    "src": mid, "dst": entry.entity_id,
                    "eid": edge_identity("CANDIDATE_FOR", mid, entry.entity_id).id,
                    "score": entry.score, "co_occurrences": entry.co_occurrences,
                    "participations": entry.participations, "run_id": run_id})
            if decision.winner is None:
                statuses.append({"vertex": mid, "status": "unresolved",
                                 "method": METHOD_UNRESOLVED,
                                 "candidates": len(candidates),
                                 "reason": (decision.reason or "")[:300]})
                continue
            graph_resolves.append({
                "src": mid, "dst": decision.winner.entity_id,
                "eid": edge_identity("RESOLVES_TO", mid, decision.winner.entity_id).id,
                "method": METHOD_GRAPH_EVIDENCE,
                "confidence": round(0.5 + 0.45 * decision.margin, 3),
                "evidence": decision.reason[:400], "candidates": len(candidates),
                "run_id": run_id})
            statuses.append({"vertex": mid, "status": "resolved",
                             "method": METHOD_GRAPH_EVIDENCE,
                             "candidates": len(candidates),
                             "reason": decision.reason[:300]})

        # Now every decision is known, so the graph can be reconciled against it.
        # Deleting earlier would have spared the ambiguous mentions, whose new
        # answers were not yet computed — they would have kept a superseded edge
        # and gained a second one beside it.
        for row in graph_resolves:
            want[row["src"]] = row["dst"]
        stale = [rid for mid, pairs in existing.items() for eid, rid in pairs
                 if mid in want and want[mid] != eid]
        if stale:
            delete_edges(client, "RESOLVES_TO", stale)
        print(f"  deleted {len(stale)} superseded RESOLVES_TO edges")

        if resolves:
            upsert_edges(client, "RESOLVES_TO", resolves, job=f"rebuild-r:{run_id}",
                         source_label=MENTION, target_label=ENTITY,
                         properties=["method", "confidence", "evidence",
                                     "candidates", "run_id"])
            pending += [edge_identity("RESOLVES_TO", r["src"], r["dst"])
                        for r in resolves]
        if graph_resolves:
            upsert_edges(client, "RESOLVES_TO", graph_resolves,
                         job=f"rebuild-gr:{run_id}", source_label=MENTION,
                         target_label=ENTITY,
                         properties=["method", "confidence", "evidence",
                                     "candidates", "run_id"])
            pending += [edge_identity("RESOLVES_TO", r["src"], r["dst"])
                        for r in graph_resolves]
        if candidate_edges:
            upsert_edges(client, "CANDIDATE_FOR", candidate_edges,
                         job=f"rebuild-cf:{run_id}", source_label=MENTION,
                         target_label=ENTITY,
                         properties=["score", "co_occurrences", "participations",
                                     "run_id"])
        if statuses:
            upsert_nodes(client, MENTION, statuses, job=f"rebuild-st:{run_id}",
                         properties=["status", "method", "candidates", "reason"])

        moved = repoint_merged_away(client, run_id, resolver, entity_ids, want)
        if moved:
            print(f"  repointed {moved} mentions off identities that were merged away")

        registry.register_many(pending)
        resolved = sum(1 for s in statuses if s["status"] == "resolved")
        print(f"  {resolved} resolved, {len(statuses) - resolved} unresolved, "
              f"{evidence.queries} graph queries")
        print(f"done in {time.perf_counter() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
