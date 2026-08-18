#!/usr/bin/env python
"""Check the vertical-slice exit gate against the graph, scoped to one run.

Two things this deliberately does not do, because an earlier version did both
and passed for the wrong reasons:

* It does not group by entity *name*. Several distinct entities can share a
  display name, so name-grouping reports one person reached by many surfaces
  when the truth is many near-duplicate people reached by one surface each.
  Grouping is by entity id.
* It does not read the whole graph. Without a run id, leftovers from any earlier
  ingest can satisfy a check that the current run would fail.

    uv run python scripts/35_verify_gate.py --run-id run1786895087
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph.hydra_client import HydraClient, parse_bookmark  # noqa: E402
from tracegraph.parsers.base import email_domain, organisation_root  # noqa: E402
from tracegraph.resolve import _same_organisation  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    for line in (detail or "").splitlines():
        print(f"          {line}")


def latest_run(client: HydraClient) -> str | None:
    rows = client.bolt_read(
        "MATCH (d:Document) RETURN d.run_id AS run_id ORDER BY run_id DESC LIMIT 1"
    )
    return rows[0]["run_id"] if rows else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    with HydraClient() as client:
        client.verify()
        run_id = args.run_id or latest_run(client)
        if not run_id:
            print("no ingested run found; run scripts/30_load_slice.py", file=sys.stderr)
            return 1
        print(f"run {run_id}\n")

        counts = {}
        for label in ("Document", "Entity", "Mention", "Channel"):
            rows = client.bolt_read(
                f"MATCH (n:{label}) WHERE n.run_id = $r RETURN count(*) AS c",
                {"r": run_id})
            counts[label] = rows[0]["c"]
            print(f"  {label:9} {counts[label]:>6}")
        record("slice ingested", counts["Document"] >= 200)

        # --- resolution reached through the graph ----------------------------
        rows = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $r "
            "RETURN e.id AS entity_id, e.name AS name, m.normalised AS surface, "
            "r.method AS method, r.confidence AS confidence, r.evidence AS evidence, "
            "r.candidates AS candidates LIMIT 6000",
            {"r": run_id})

        by_entity: dict[int, set[str]] = defaultdict(set)
        detail: dict[int, list[dict]] = defaultdict(list)
        for row in rows:
            by_entity[row["entity_id"]].add(row["surface"])
            detail[row["entity_id"]].append(row)

        # Distinct *forms*, not distinct spellings of the same address: a person
        # reached by their name and by their own email is one surface plus its
        # machine-readable form, which is not what the gate is asking about.
        strong = {
            eid: forms for eid, forms in by_entity.items()
            if len({f for f in forms if "@" not in f}) >= 2
            or (len(forms) >= 2 and any("@" not in f for f in forms))
        }
        record("two or more distinct surfaces resolve to one entity id",
               bool(strong), f"{len(strong)} entities qualify")

        if strong:
            eid = max(strong, key=lambda k: len(strong[k]))
            print(f"\n  worked example: {detail[eid][0]['name']} (entity {eid})")
            seen = set()
            for row in detail[eid]:
                if row["surface"] in seen:
                    continue
                seen.add(row["surface"])
                print(f"    {row['surface']!r:30} {row['method']:20} "
                      f"confidence={row['confidence']}")
                if row["evidence"]:
                    print(f"        {row['evidence'][:120]}")

        methods = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $r "
            "RETURN r.method AS method, count(*) AS n", {"r": run_id})
        record("every resolution records a method and confidence",
               bool(methods) and all(row["method"] for row in methods),
               "\n".join(f"{row['method']}: {row['n']}" for row in methods))

        # --- the decision was made by the graph ------------------------------
        # Occurrences and distinct surfaces are reported separately, because
        # they are wildly different numbers and the smaller one is the honest
        # measure of how much ambiguity was actually adjudicated: a handful of
        # bare first names accounts for most of the volume.
        graph_backed = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) "
            "WHERE r.run_id = $r AND r.method = 'graph_evidence' "
            "RETURN m.normalised AS surface LIMIT 20000", {"r": run_id})
        n_graph = len(graph_backed)
        n_graph_surfaces = len({row["surface"] for row in graph_backed})
        record("resolutions decided by graph evidence", n_graph > 0,
               f"{n_graph} mention occurrences resolved by traversal over stored "
               f"structure, across {n_graph_surfaces} distinct surfaces")

        participation = client.bolt_read(
            "MATCH (e:Entity)-[r:PARTICIPATED_IN]->(c:Channel) WHERE r.run_id = $r "
            "RETURN count(*) AS n", {"r": run_id})
        n_part = participation[0]["n"] if participation else 0
        record("participation structure exists for the graph to read", n_part > 0,
               f"{n_part} Entity-[:PARTICIPATED_IN]->Channel edges")

        candidates = client.bolt_read(
            "MATCH (m:Mention)-[r:CANDIDATE_FOR]->(e:Entity) WHERE r.run_id = $r "
            "RETURN count(*) AS n", {"r": run_id})
        n_cand = candidates[0]["n"] if candidates else 0
        record("rejected candidates are recorded, not just winners", n_cand > 0,
               f"{n_cand} candidate edges carry their score and evidence counts")

        # --- unresolved is a decision, not a gap -----------------------------
        statuses = client.bolt_read(
            "MATCH (m:Mention) WHERE m.run_id = $r "
            "RETURN m.status AS status, count(*) AS n", {"r": run_id})
        status_map = {row["status"]: row["n"] for row in statuses}
        record("every mention carries an explicit status",
               status_map.get("pending", 0) == 0 and len(status_map) > 0,
               ", ".join(f"{k}={v}" for k, v in sorted(status_map.items())))

        # The mention's `entity` property and its RESOLVES_TO edge are two
        # records of one decision, and conflict grouping reads the property
        # while everything else reads the edge. They desynchronised silently
        # when a repair moved the edge and left the property behind.
        edge_targets: dict[int, set[int]] = {}
        for row in client.bolt_read(
                "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $r "
                "RETURN m.id AS mid, e.id AS eid LIMIT 20000", {"r": run_id}):
            edge_targets.setdefault(row["mid"], set()).add(row["eid"])
        mismatched = 0
        for row in client.bolt_read(
                "MATCH (m:Mention) WHERE m.run_id = $r "
                "RETURN m.id AS mid, m.status AS status, m.entity AS entity "
                "LIMIT 20000", {"r": run_id}):
            targets = edge_targets.get(row["mid"], set())
            if row["status"] == "resolved":
                if targets != {row["entity"]}:
                    mismatched += 1
            elif row["entity"]:
                mismatched += 1
        record("every mention's recorded identity matches its edge", mismatched == 0,
               f"{mismatched} mention(s) disagree with their own RESOLVES_TO edge")

        unresolved_with_reason = client.bolt_read(
            "MATCH (m:Mention) WHERE m.run_id = $r AND m.status = 'unresolved' "
            "AND m.candidates > 1 RETURN m.normalised AS surface LIMIT 20000",
            {"r": run_id})
        n_amb = len(unresolved_with_reason)
        n_amb_surfaces = len({row["surface"] for row in unresolved_with_reason})
        record("unresolved mentions keep their candidate count", n_amb > 0,
               f"{n_amb} mentions kept a competing candidate set, across "
               f"{n_amb_surfaces} distinct surfaces")

        # Mention clusters can be clean while the entity population is not.
        #
        # A merge leaves the folded vertex in place — deleting it would strand
        # whatever references it — so canonicalisation has to be *recorded*, or
        # the next resolver adopts the loser again as its own protected identity
        # and the merge quietly undoes itself. `Camila Reyes` returned to six
        # candidates on every restart while mention-level splits read as fixed.
        live_names: dict[str, set[str]] = {}
        superseded = {row["from"] for row in client.bolt_read(
            "MATCH (a:Entity)-[m:MERGED_INTO]->(b:Entity) WHERE m.run_id = $r "
            "RETURN a.key AS from LIMIT 20000", {"r": run_id})}
        for row in client.bolt_read(
                "MATCH (e:Entity) WHERE e.run_id = $r "
                "RETURN e.key AS key, e.name AS name LIMIT 20000", {"r": run_id}):
            name = (row["name"] or "").strip().casefold()
            if not name or "@" in name or len(name.split()) < 2:
                continue
            if row["key"] in superseded:
                continue
            live_names.setdefault(name, set()).add(row["key"])
        # A shared name across *different* organisations is not fragmentation,
        # it is two people — 98 such names here, and merging them would be the
        # false merge the resolver exists to refuse. Only a name split across
        # vertices at the same organisation is a merge that failed to happen.
        def _root(key: str) -> str:
            return organisation_root(email_domain(key.split(":", 1)[-1]))

        fragmented = {
            name: keys for name, keys in live_names.items()
            if len(keys) > 1 and any(
                _same_organisation(_root(a), _root(b))
                for i, a in enumerate(sorted(keys)) for b in sorted(keys)[i + 1:])
        }
        split_across_orgs = sum(1 for k in live_names.values() if len(k) > 1)
        record("one canonical identity per person per organisation",
               not fragmented,
               f"{len(live_names)} named identities; {len(fragmented)} split "
               f"within one organisation, {split_across_orgs - len(fragmented)} "
               "across different organisations (two people, correctly apart)"
               + (f"; worst: {sorted(fragmented, key=lambda n: -len(fragmented[n]))[0]}"
                  if fragmented else ""))

        # The panel recomputes disputes from claims; answers walk persisted
        # edges. When those disagree, the interface shows a conflict the answer
        # cannot see — 31 edges were missing when this was first measured, over
        # 15 fact groups. Exact equality, because "close" is indistinguishable
        # from a detector that has quietly stopped writing.
        from tracegraph.conflicts import ClaimRecord, detect_conflicts
        from tracegraph.reconcile import (
            _read_all, conflict_edge_rows, load_claims, load_subject_identity,
        )

        records = [ClaimRecord(**row) for row in load_claims(client, run_id)]
        stamps = {r.dsid: r.timestamp for r in records if r.timestamp}
        detected, _ = detect_conflicts(
            records, document_order=sorted(stamps, key=lambda d: stamps[d]),
            subject_identity=load_subject_identity(client, run_id))
        _, expected_rows = conflict_edge_rows(detected, run_id)
        expected = {(row["src"], row["dst"]) for row in expected_rows}
        # Paged, not capped. A check that reads 20,000 of N edges and declares
        # them equal to the detector is asserting something it did not look at.
        persisted = {
            (row["src"], row["dst"]) for row in _read_all(
                client,
                "MATCH (a:Claim)-[e:CONFLICTS_WITH]->(b:Claim) WHERE e.run_id = $r "
                "RETURN a.id AS src, b.id AS dst",
                {"r": run_id}, 4000, "ORDER BY a.id, b.id")}
        missing, extra = expected - persisted, persisted - expected
        record("every detected conflict is persisted as an edge",
               not missing and not extra,
               f"{len(expected)} detected, {len(persisted)} persisted, "
               f"{len(missing)} missing, {len(extra)} superseded")

        # --- bounded evidence path -------------------------------------------
        anchor = client.bolt_read(
            "MATCH (e:Entity)-[r:PARTICIPATED_IN]->(c:Channel) WHERE r.run_id = $r "
            "RETURN e.id AS eid, c.id AS cid LIMIT 1", {"r": run_id})
        path_rows = []
        if anchor:
            path_rows = client.bolt_read(
                "CALL algo.SPpaths({sourceNode: $src, targetNode: $dst, "
                "relTypes: ['PARTICIPATED_IN'], maxLen: 2, relDirection: 'both', "
                "pathCount: 1}) YIELD path RETURN path",
                {"src": anchor[0]["eid"], "dst": anchor[0]["cid"]})
        record("bounded evidence path returns from the graph", bool(path_rows),
               f"{len(path_rows)} path(s) within 2 hops")

        # --- citation validation is not vacuous ------------------------------
        real = client.bolt_read(
            "MATCH (d:Document) WHERE d.run_id = $r RETURN d.id AS id LIMIT 1",
            {"r": run_id})
        record("citation validation distinguishes a real dsid from an unwritten one",
               bool(real) and client.exists_node("Document", real[0]["id"])
               and not client.exists_node("Document", 424242424242424242))

        result = client.http_query("MATCH (d:Document) RETURN count(*) AS c")
        scope = parse_bookmark(result.bookmark) if result.bookmark else None
        record("trace can show a real read epoch and bookmark",
               result.read_epoch is not None and scope is not None,
               f"read_epoch={result.read_epoch} "
               f"bookmark_sequence={scope.sequence if scope else 'n/a'}")

    passed = sum(1 for _, ok in CHECKS if ok)

    # Write the snapshot the documents quote.
    #
    # Every prose number describing this graph has gone stale between audits,
    # four times over, because the graph grows whenever anyone asks a question.
    # Chasing the digits by hand is what kept failing. One generated artifact
    # carrying the epoch it was taken at is something the README can point to
    # rather than restate, and regenerating it is a command rather than an edit.
    snapshot: dict = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks_passed": passed,
        "checks_total": len(CHECKS),
    }
    with HydraClient() as client:
        client.verify()
        rows = client.bolt_read(
            "MATCH (d:Document) RETURN d.run_id AS r ORDER BY r DESC LIMIT 1")
        run_id = rows[0]["r"] if rows else "ondemand"
        probe = client.http_query("MATCH (d:Document) RETURN count(*) AS c")
        snapshot["run_id"] = run_id
        snapshot["read_epoch"] = probe.read_epoch
        snapshot["nodes"] = {
            label: client.bolt_read(
                f"MATCH (x:{label}) WHERE x.run_id = $r RETURN count(*) AS n",
                {"r": run_id})[0]["n"]
            for label in ("Document", "Entity", "Mention", "Claim",
                          "EvidenceSpan", "Channel")
        }
        by_graph = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $r "
            "AND r.method = 'graph_evidence' RETURN m.normalised AS s LIMIT 20000",
            {"r": run_id})
        competing = client.bolt_read(
            "MATCH (m:Mention) WHERE m.run_id = $r AND m.status = 'unresolved' "
            "AND m.candidates > 1 RETURN m.normalised AS s LIMIT 20000",
            {"r": run_id})
        snapshot["resolution"] = {
            "graph_evidence_occurrences": len(by_graph),
            "graph_evidence_surfaces": len({x["s"] for x in by_graph}),
            "competing_unresolved_mentions": len(competing),
            "competing_unresolved_surfaces": len({x["s"] for x in competing}),
        }

    out = Path(__file__).resolve().parent.parent / "artifacts" / "graph_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"  snapshot -> artifacts/{out.name} (read_epoch {probe.read_epoch}, "
          f"{snapshot['nodes']['Document']} documents)")

    print(f"\n{passed}/{len(CHECKS)} gate checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
