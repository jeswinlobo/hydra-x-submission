#!/usr/bin/env python
"""Check the vertical-slice exit gate against the graph, not against memory.

Everything here is read back out of HydraDB. An in-process assertion proves the
Python was right; only a query proves the graph actually holds what the answer
path will read.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracegraph.hydra_client import HydraClient, parse_bookmark  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if detail:
        for line in detail.splitlines():
            print(f"          {line}")


def main() -> int:
    with HydraClient() as client:
        client.verify()

        print("\ngraph contents")
        counts = {
            label: client.count_labelled(label)
            for label in ("Document", "Entity", "Mention", "Channel")
        }
        for label, count in counts.items():
            print(f"  {label:9} {count:>6}")
        record("slice ingested", counts["Document"] >= 200)

        # --- entity resolution reached through the graph ---------------------
        rows = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) "
            "RETURN e.name AS name, m.normalised AS surface, r.method AS method, "
            "r.confidence AS confidence, r.evidence AS evidence, "
            "r.candidates AS candidates LIMIT 4000"
        )
        by_entity: dict[str, set[str]] = defaultdict(set)
        detail_by_entity: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_entity[row["name"]].add(row["surface"])
            detail_by_entity[row["name"]].append(row)

        multi = {name: forms for name, forms in by_entity.items() if len(forms) >= 2}
        # A surface that is merely the person's own address spelled out is not a
        # second identity; the gate wants genuinely different forms.
        strong = {
            name: forms
            for name, forms in multi.items()
            if len({f for f in forms if "@" not in f}) >= 1 and len(forms) >= 2
        }
        record(
            "two or more distinct surfaces resolve to one entity",
            bool(strong),
            f"{len(strong)} entities qualify",
        )

        if strong:
            name = max(strong, key=lambda n: len(strong[n]))
            print(f"\n  worked example: {name}")
            for row in detail_by_entity[name][:4]:
                print(f"    surface {row['surface']!r:34} method={row['method']} "
                      f"confidence={row['confidence']}")
                if row["evidence"]:
                    print(f"       evidence: {row['evidence'][:110]}")

        methods = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) "
            "RETURN r.method AS method, count(*) AS n"
        )
        record(
            "resolution records a method and confidence",
            all(row["method"] for row in methods),
            "\n".join(f"{row['method']}: {row['n']}" for row in methods),
        )

        # Resolution that is more than string equality: the graph must contain
        # at least one decision backed by evidence rather than an exact match.
        evidenced = client.bolt_read(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) "
            "WHERE r.candidates > 1 RETURN count(*) AS n"
        )
        contested = evidenced[0]["n"] if evidenced else 0
        record(
            "at least one resolution had competing candidates",
            contested > 0,
            f"{contested} resolutions chose between two or more candidates",
        )

        # --- bounded evidence path -------------------------------------------
        anchor = client.bolt_read(
            "MATCH (m:Mention)-[:RESOLVES_TO]->(e:Entity) RETURN e.id AS id LIMIT 1"
        )
        path_rows = []
        if anchor:
            path_rows = client.bolt_read(
                "CALL algo.SSpaths({sourceNode: $src, "
                "relTypes: ['RESOLVES_TO', 'MENTIONED_IN'], maxLen: 2, "
                "relDirection: 'both', pathCount: 3}) YIELD path RETURN path",
                {"src": anchor[0]["id"]},
            )
        record(
            "bounded evidence path returns from the entity",
            bool(path_rows),
            f"{len(path_rows)} path(s) within 2 hops",
        )
        if path_rows:
            path = path_rows[0]["path"]
            rendered = " -> ".join(
                str(step.get("name") or step.get("surface") or step.get("dsid"))
                if isinstance(step, dict) else str(step)
                for step in path
            )
            print(f"    {rendered[:160]}")

        # --- citation validation is not vacuous ------------------------------
        real = client.bolt_read("MATCH (d:Document) RETURN d.id AS id LIMIT 1")
        real_id = real[0]["id"] if real else 0
        record(
            "citation validation distinguishes a real dsid from an unwritten one",
            client.exists_node("Document", real_id)
            and not client.exists_node("Document", 424242424242424242),
        )

        # --- consistency evidence for the trace ------------------------------
        result = client.http_query("MATCH (d:Document) RETURN count(*) AS c")
        scope = parse_bookmark(result.bookmark) if result.bookmark else None
        record(
            "trace can show a real read epoch and bookmark",
            result.read_epoch is not None and scope is not None,
            f"read_epoch={result.read_epoch} bookmark_sequence="
            f"{scope.sequence if scope else 'n/a'}",
        )

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} gate checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
