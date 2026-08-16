#!/usr/bin/env python
"""Build the vertical slice: parse, resolve, and load a bounded neighbourhood.

The slice is small on purpose. Its job is to prove the whole path end to end —
Parquet to parser to resolution to graph to a bounded evidence path — not to
cover the corpus. Bulk ingestion only starts once this works and has been
benchmarked.

Selection is channel-driven rather than random: a Slack channel plus the email
around it is a neighbourhood where the same people appear under different
surfaces, which is what makes resolution testable at all.

    uv run python scripts/30_load_slice.py --channel eng-runtime --limit 600
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq  # noqa: E402

from tracegraph import config  # noqa: E402
from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ids import edge_id, node_id  # noqa: E402
from tracegraph.loader import Checkpointer, upsert_edges, upsert_nodes  # noqa: E402
from tracegraph.parsers import normalise_content, parse_document  # noqa: E402
from tracegraph.parsers.base import BOT, PERSON  # noqa: E402
from tracegraph.resolve import METHOD_UNRESOLVED, Resolver  # noqa: E402

DOC = "Document"
ENTITY = "Entity"
MENTION = "Mention"
CHANNEL = "Channel"


def select_slice(channel: str, limit: int, email_limit: int) -> list[dict]:
    """Documents from one Slack channel, plus internal email for identities.

    Slack alone cannot resolve anything: its speakers are bare handles. The
    email is what supplies names and addresses, so the slice deliberately spans
    both sources.
    """
    parquet = pq.ParquetFile(config.DOCUMENTS_PARQUET)
    slack: list[dict] = []
    gmail: list[dict] = []

    for batch in parquet.iter_batches(
        batch_size=4000, columns=["doc_id", "source_type", "title", "content"]
    ):
        data = batch.to_pydict()
        source = data["source_type"][0]
        if source not in ("slack", "gmail"):
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
    ap.add_argument("--limit", type=int, default=400, help="Slack documents")
    ap.add_argument("--email-limit", type=int, default=400, help="Gmail documents")
    ap.add_argument("--job", default="slice", help="checkpoint job name")
    args = ap.parse_args()

    print(f"selecting slice: #{args.channel} + internal email")
    docs = select_slice(args.channel, args.limit, args.email_limit)
    by_source = Counter(d["source_type"] for d in docs)
    print(f"  {len(docs)} documents {dict(by_source)}")
    if not docs:
        print("no documents matched; try another channel", file=sys.stderr)
        return 1

    # --- parse ---------------------------------------------------------------
    parsed = {}
    bodies = {}
    bad_offsets = 0
    for d in docs:
        # Offsets index into the normalised body, so verification has to use it
        # too. This is also the body an evidence span is later checked against.
        body = normalise_content(d["content"])
        bodies[d["doc_id"]] = body
        p = parse_document(d["doc_id"], d["source_type"], d["title"], d["content"])
        verified = p.verified_mentions(body)
        bad_offsets += len(p.mentions) - len(verified)
        p.mentions = verified
        parsed[d["doc_id"]] = p

    mention_count = sum(len(p.mentions) for p in parsed.values())
    print(f"  parsed: {mention_count} mentions with verified offsets, "
          f"{bad_offsets} rejected for bad offsets")

    # --- resolve -------------------------------------------------------------
    resolver = Resolver()
    for d in docs:
        p = parsed[d["doc_id"]]
        resolver.observe(d["doc_id"], d["source_type"], p.mentions,
                         channel=p.attributes.get("channel"))
    print(f"  identities built from email: {len(resolver.people)}")

    resolutions = []
    for d in docs:
        p = parsed[d["doc_id"]]
        channel = p.attributes.get("channel")
        for m in p.mentions:
            resolutions.append((d["doc_id"], m,
                                resolver.resolve_mention(m, d["doc_id"], channel)))

    # Second pass. The first pass could only use email, which carries no
    # channel, so every ambiguous handle fell through to unresolved. Confident
    # resolutions inside Slack documents establish who actually participates
    # where, which is the evidence the graph tier needs.
    channel_by_doc = {
        doc_id: p.attributes["channel"]
        for doc_id, p in parsed.items()
        if p.attributes.get("channel")
    }
    learned = resolver.learn_participation(
        (r for _, _, r in resolutions), channel_by_doc
    )
    if learned:
        print(f"  learned {learned} channel participations; re-resolving")
        resolutions = [
            (doc_id, m, resolver.resolve_mention(m, doc_id,
                                                 channel_by_doc.get(doc_id)))
            for doc_id, m, _ in resolutions
        ]

    by_method = Counter(r.method for _, _, r in resolutions)
    resolved = sum(1 for _, _, r in resolutions if r.resolved)
    print(f"  resolved {resolved}/{len(resolutions)} mentions {dict(by_method)}")

    # --- load ----------------------------------------------------------------
    checkpointer = Checkpointer()
    with HydraClient() as client:
        client.verify()

        doc_rows = [
            {"vertex": node_id(DOC, d["doc_id"]), "dsid": d["doc_id"],
             "source_type": d["source_type"], "title": d["title"][:500]}
            for d in docs
        ]
        upsert_nodes(client, DOC, doc_rows, job=args.job,
                     properties=["dsid", "source_type", "title"],
                     checkpointer=checkpointer)

        entity_rows = [
            {"vertex": node_id(ENTITY, person.key), "key": person.key,
             "name": person.display_name, "kind": PERSON,
             "emails": ";".join(sorted(person.emails)),
             "domains": ";".join(sorted(person.domains))}
            for person in resolver.people.values()
        ]
        upsert_nodes(client, ENTITY, entity_rows, job=args.job,
                     properties=["key", "name", "kind", "emails", "domains"],
                     checkpointer=checkpointer)

        channels = sorted({p.attributes["channel"] for p in parsed.values()
                           if p.attributes.get("channel")})
        if channels:
            upsert_nodes(
                client, CHANNEL,
                [{"vertex": node_id(CHANNEL, c), "name": c} for c in channels],
                job=args.job, properties=["name"], checkpointer=checkpointer,
            )

        # Mentions are nodes so provenance stays independently inspectable: one
        # surface, its offsets, and the decision made about it.
        mention_rows = []
        mentioned_in = []
        resolves_to = []
        sent_in = []
        for doc_id, mention, resolution in resolutions:
            natural = f"{doc_id}:{mention.start}:{mention.end}"
            mid = node_id(MENTION, natural)
            did = node_id(DOC, doc_id)
            mention_rows.append({
                "vertex": mid, "surface": mention.surface,
                "normalised": mention.surface.casefold(),
                "kind": mention.kind, "role": mention.role,
                "start": mention.start, "end": mention.end, "dsid": doc_id,
            })
            mentioned_in.append({
                "src": mid, "dst": did,
                "eid": edge_id("MENTIONED_IN", mid, did),
                "role": mention.role,
            })
            if resolution.resolved:
                eid_target = node_id(ENTITY, resolution.person_key)
                resolves_to.append({
                    "src": mid, "dst": eid_target,
                    "eid": edge_id("RESOLVES_TO", mid, eid_target),
                    "method": resolution.method,
                    "confidence": resolution.confidence,
                    "evidence": resolution.evidence[:400],
                    "candidates": len(resolution.candidates),
                })

        for doc_id, parsed_doc in parsed.items():
            channel = parsed_doc.attributes.get("channel")
            if channel:
                did = node_id(DOC, doc_id)
                cid = node_id(CHANNEL, channel)
                sent_in.append({"src": did, "dst": cid,
                                "eid": edge_id("SENT_IN", did, cid)})

        upsert_nodes(client, MENTION, mention_rows, job=args.job,
                     properties=["surface", "normalised", "kind", "role",
                                 "start", "end", "dsid"],
                     checkpointer=checkpointer)
        upsert_edges(client, "MENTIONED_IN", mentioned_in, job=args.job,
                     source_label=MENTION, target_label=DOC,
                     properties=["role"], checkpointer=checkpointer)
        upsert_edges(client, "RESOLVES_TO", resolves_to, job=args.job,
                     source_label=MENTION, target_label=ENTITY,
                     properties=["method", "confidence", "evidence", "candidates"],
                     checkpointer=checkpointer)
        if sent_in:
            upsert_edges(client, "SENT_IN", sent_in, job=args.job,
                         source_label=DOC, target_label=CHANNEL,
                         checkpointer=checkpointer)

        print("\ngraph:")
        for label in (DOC, ENTITY, MENTION, CHANNEL):
            print(f"  {label:9} {client.count_labelled(label):>6}")

        # --- the exit-gate check: two surfaces, one entity ------------------
        surfaces = defaultdict(set)
        for _, mention, resolution in resolutions:
            if resolution.resolved and mention.kind != BOT:
                surfaces[resolution.person_key].add(mention.surface.casefold())

        multi = {k: v for k, v in surfaces.items() if len(v) >= 2}
        print(f"\nentities reached by two or more distinct surfaces: {len(multi)}")
        for key, forms in list(multi.items())[:5]:
            person = resolver.people[key]
            print(f"  {person.display_name:24} <- {sorted(forms)}")

        ambiguous = resolver.ambiguous_surfaces()
        print(f"\nsurfaces left ambiguous by design: {len(ambiguous)}")
        for handle, cands in list(ambiguous.items())[:5]:
            names = [resolver.people[c].display_name for c in cands[:4]]
            print(f"  {handle:16} {len(cands)} candidates: {names}")

    checkpointer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
