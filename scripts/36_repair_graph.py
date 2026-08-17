#!/usr/bin/env python
"""Repair identity damage and undecided mentions, and report what it found.

Two states this fixes, both of which the verification gate can see but neither
of which it could previously do anything about.

**Polluted identities.** An entity's `emails` field is a joined list, and two
faults fed each other. It was cut to a byte length rather than at a separator,
so the last address could be severed — `grace_oconnor@redwood.ai` became the
entry `grace_oco`. And on-demand ingestion merged same-named people after their
vertices already existed, so Priya Sharma at procureco.com absorbed
priya@mediloop.com: a different person at a different company. The absorbed
address then made the two look like one organisation, which kept the merge
alive on every later pass.

**Mentions nobody decided about.** A mention is written `pending` and given its
status at the end of the resolution pass. If anything in between fails, the
error is swallowed so one document cannot fail a question — and the mention
keeps a status that means "nobody looked".

    uv run python scripts/36_repair_graph.py            # report only
    uv run python scripts/36_repair_graph.py --apply    # write the repairs

Reporting is the default because this writes to the graph.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph.hydra_client import HydraClient  # noqa: E402
from tracegraph.ingest import OnDemandIngestor  # noqa: E402
from tracegraph.loader import upsert_nodes  # noqa: E402
from tracegraph.parsers.base import email_domain, organisation_root  # noqa: E402
from tracegraph.resolve import pack  # noqa: E402

ENTITY = "Entity"


def live_entities(client: HydraClient, run_id: str) -> set[int]:
    """Ids of entities something still resolves to.

    One traversal rather than one per entity: anchoring a RESOLVES_TO query on a
    single vertex is cheap, but a thousand of them is not — the cost of a
    relationship walk tracks the anchor label, not the rows returned (see
    docs/engine-notes.md).
    """
    return {row["eid"] for row in client.bolt_read(
        "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $run "
        "RETURN DISTINCT e.id AS eid LIMIT 20000", {"run": run_id})}


def audit_entities(client: HydraClient, run_id: str) -> list[dict]:
    """Entities holding an address that provably is not theirs.

    Two kinds, and only two, because this repairs damage rather than relitigating
    identity decisions:

    * a **severed fragment** — an entry with no `@`, which no parser can produce
      and which only exists because a joined list was cut mid-address;
    * a **stranded merge** — an address that was folded in even though it still
      has its own Entity vertex, so the graph holds both, and new mentions of it
      resolve to whichever one survived the fold.

    Deliberately *not* repaired: an address from another organisation whose own
    vertex does not exist. That is the bulk loader's merge-by-name behaviour,
    which predates this script — 178 addresses on this corpus. Whether those are
    one person who changed employer or several people sharing a name cannot be
    settled from the graph alone, and rewriting them here would be a guess
    dressed as a repair.
    """
    rows = client.bolt_read(
        "MATCH (e:Entity) WHERE e.run_id = $r RETURN e.id AS id, e.key AS key, "
        "e.name AS name, e.emails AS emails, e.domains AS domains ORDER BY e.key",
        {"r": run_id})
    keys = {row["key"]: row["id"] for row in rows if row["key"]}
    live = live_entities(client, run_id)

    damaged = []
    for row in rows:
        key = row["key"] or ""
        own = key.split(":", 1)[1] if ":" in key else ""

        # An identity nothing resolves to any more cannot mislead an answer,
        # whatever its properties still say. It is the residue of a merge, and
        # deleting it would strand anything else that references it.
        if row["id"] not in live:
            continue

        emails = [e for e in (row["emails"] or "").split(";") if e]
        kept, fragments, stranded, merged_in = [], [], [], []
        for email in emails:
            if "@" not in email:
                fragments.append(email)
            elif (email != own and f"email:{email}" in keys
                  and keys[f"email:{email}"] in live):
                # The absorbed address has its own vertex *and* mentions are
                # still resolving to it, so the graph holds this person twice
                # and answers can be attributed to either. A vertex nothing
                # points at any more is an orphan, not damage, and is left be —
                # deleting it would strand whatever else references it.
                stranded.append(email)
            else:
                kept.append(email)
                if email != own:
                    merged_in.append(email)
        if own and own not in kept:
            kept.append(own)

        if fragments or stranded:
            damaged.append({
                "key": key, "name": row["name"], "kept": sorted(set(kept)),
                "fragments": fragments, "stranded": stranded,
                "merged_in": merged_in,
            })
    return damaged


def repair_entities(client: HydraClient, run_id: str, damaged: list[dict]) -> int:
    rows = [{
        "vertex": None, "key": d["key"],
        "emails": pack(sorted(d["kept"]), 400),
        "domains": pack(sorted({email_domain(e) for e in d["kept"] if e}), 200),
    } for d in damaged]

    # The vertex id is looked up rather than re-derived, so this repairs the
    # vertex that exists rather than minting one beside it.
    for row in rows:
        found = client.bolt_read(
            "MATCH (e:Entity {key: $k}) WHERE e.run_id = $r RETURN e.id AS id",
            {"k": row["key"], "r": run_id})
        row["vertex"] = found[0]["id"] if found else None
    rows = [r for r in rows if r["vertex"] is not None]

    if rows:
        upsert_nodes(client, ENTITY, rows, job=f"repair-entities:{run_id}",
                     properties=["emails", "domains"])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the repairs")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

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
        print(f"run {run_id}\n")

        # --- identities ------------------------------------------------------
        damaged = audit_entities(client, run_id)
        print(f"entities holding an address that is provably not theirs: "
              f"{len(damaged)}")
        for entry in damaged[:12]:
            print(f"  {entry['key']}  ({entry['name']})")
            for email in entry["stranded"]:
                print(f"      stranded merge:   {email}  (has its own vertex)")
            for email in entry["fragments"]:
                print(f"      severed fragment: {email}")
        if damaged and args.apply:
            print(f"  repaired {repair_entities(client, run_id, damaged)} entities")

        # --- mentions --------------------------------------------------------
        ingestor = OnDemandIngestor(client, run_id)
        try:
            pending = ingestor.pending_documents()
            print(f"\ndocuments holding an undecided mention: {len(pending)}")
            if pending and args.apply:
                reports = ingestor.repair_pending()
                resolved = sum(r.resolved for r in reports)
                unresolved = sum(r.unresolved for r in reports)
                failed = [r for r in reports if r.error]
                print(f"  decided {resolved} resolved, {unresolved} unresolved")
                for report in failed[:5]:
                    print(f"  still failing: {report.dsid} — {report.error}")
        finally:
            ingestor.close()

        if not args.apply and (damaged or pending):
            print("\nre-run with --apply to write these repairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
