"""Contract tests pinning HydraDB behaviour the ingestion and answer code rely on.

Each test protects one assumption recorded in docs/engine-notes.md. They exist
because most of these behaviours fail *silently* — a phantom row, a duplicated
edge, a batch quietly rejected — and a silent failure in the graph invalidates
the guarantee the whole submission rests on.

Skipped cleanly when no node is running; see tests/conftest.py.
"""

from __future__ import annotations

import time

import pytest
from neo4j.exceptions import Neo4jError

from tracegraph.hydra_client import parse_bookmark

pytestmark = pytest.mark.live


# --- Transports -------------------------------------------------------------


def test_both_transports_answer(bolt, http):
    """A listening port is not proof; a round-tripped query is."""
    assert bolt.run("MATCH (n:NoSuchLabelAnywhere) RETURN count(*) AS c")[0]["c"] == 0
    assert http.query("MATCH (n:NoSuchLabelAnywhere) RETURN count(*) AS c")["rows"]


def test_http_response_carries_read_epoch_and_parsable_bookmark(http):
    """The trace shows a real consistency position, so it has to be present.

    PLAN.md allowed for read_epoch being unavailable and for the bookmark format
    being unparsed. Both are available on HTTP, so the trace can display them
    instead of rendering a fabricated zero.
    """
    payload = http.query("MATCH (n:NoSuchLabelAnywhere) RETURN count(*) AS c")

    assert "read_epoch" in payload
    assert isinstance(payload["read_epoch"], int)

    scope = parse_bookmark(payload["bookmark"])
    assert scope is not None, f"unparsable bookmark {payload['bookmark']!r}"
    assert scope.namespace and scope.graph and scope.cell
    assert isinstance(scope.sequence, int)


# --- The phantom-id contract ------------------------------------------------


def test_bare_id_match_returns_a_phantom_row(bolt, run_scope):
    """A bare `{id: N}` pattern is an address lookup, not an existence check.

    This is the highest-consequence engine behaviour in the project. Citation
    validation asks "does this document exist in the graph"; written without a
    label the answer is yes for every id in the 63-bit space, and the claim that
    every returned citation is valid becomes vacuously true.
    """
    never_written = run_scope.node_id("NeverWritten", "no-such-document")

    phantom = bolt.run("MATCH (n {id: $id}) RETURN n.id AS id", {"id": never_written})
    assert phantom == [{"id": never_written}], (
        "engine no longer returns a phantom row for an unwritten id; "
        "re-check whether exists_node still needs its label"
    )
    counted = bolt.run("MATCH (n {id: $id}) RETURN count(*) AS c", {"id": never_written})
    assert counted[0]["c"] == 1


def test_labelled_match_filters_correctly(bolt, run_scope, upsert_nodes):
    """Adding a label forces hydration, which is what makes filtering work."""
    label = run_scope.label("Phantom")
    written = run_scope.node_id(label, "written")
    never_written = run_scope.node_id(label, "never-written")
    upsert_nodes(label, [{"vertex": written, "name": "present"}])

    assert bolt.run(
        f"MATCH (n:{label} {{id: $id}}) RETURN n.id AS id", {"id": written}
    ) == [{"id": written}]
    assert bolt.run(
        f"MATCH (n:{label} {{id: $id}}) RETURN n.id AS id", {"id": never_written}
    ) == []


# --- Write forms ------------------------------------------------------------


def test_single_vertex_merge_outside_unwind_is_rejected(bolt, run_scope):
    """Every vertex upsert must be a batch, even a batch of one."""
    with pytest.raises(Neo4jError) as excinfo:
        bolt.run(
            "MERGE (n {id: $id}) SET n:Solo, n.name = $name",
            {"id": run_scope.node_id("Solo", "x"), "name": "x"},
        )
    assert "not supported" in str(excinfo.value).lower()


def test_label_folded_into_merge_pattern_is_rejected(bolt, run_scope):
    """The MERGE pattern is the identity being matched, so nothing else goes in it."""
    with pytest.raises(Neo4jError):
        bolt.run(
            "UNWIND $rows AS row MERGE (n:Folded {id: row.vertex, name: row.name})",
            {"rows": [{"vertex": run_scope.node_id("Folded", "x"), "name": "x"}]},
        )


def test_unwind_vertex_upsert_is_idempotent(bolt, run_scope, upsert_nodes):
    """Replay must not change the graph.

    A MERGE that changes nothing still commits, so resume correctness cannot be
    read off created-counts; it comes from the id being deterministic.
    """
    label = run_scope.label("Replay")
    rows = [
        {"vertex": run_scope.node_id(label, f"doc{i}"), "name": f"doc{i}"}
        for i in range(20)
    ]

    upsert_nodes(label, rows)
    first = bolt.run(f"MATCH (n:{label}) RETURN count(*) AS c")[0]["c"]
    upsert_nodes(label, rows)
    second = bolt.run(f"MATCH (n:{label}) RETURN count(*) AS c")[0]["c"]

    assert first == len(rows)
    assert second == first


def test_edge_merge_on_deterministic_id_does_not_duplicate(bolt, run_scope, upsert_nodes):
    """MERGE keyed on the edge id is what makes a resumed ingest safe.

    CREATE would append a second parallel edge on every replay, turning one
    piece of evidence into several and corroborating a claim with itself.
    """
    label = run_scope.label("EdgeEnd")
    src = run_scope.node_id(label, "src")
    dst = run_scope.node_id(label, "dst")
    upsert_nodes(label, [{"vertex": src, "name": "src"}, {"vertex": dst, "name": "dst"}])

    eid = run_scope.edge_id("LINKS", src, dst)
    statement = (
        "UNWIND $rows AS row "
        f"MATCH (s:{label} {{id: row.src}}), (d:{label} {{id: row.dst}}) "
        "MERGE (s)-[r:LINKS {id: row.eid}]->(d) SET r.method = row.method"
    )
    rows = [{"src": src, "dst": dst, "eid": eid, "method": "test"}]

    bolt.run(statement, {"rows": rows})
    bolt.run(statement, {"rows": rows})

    count = bolt.run(
        f"MATCH (s:{label} {{id: $src}})-[r:LINKS]->(d:{label}) RETURN count(*) AS c",
        {"src": src},
    )[0]["c"]
    assert count == 1, "edge MERGE on a deterministic id duplicated on replay"


def test_63_bit_ids_and_values_round_trip_exactly(bolt, run_scope):
    """Ids are masked to 63 bits; the largest one must survive the wire."""
    label = run_scope.label("BigInt")
    biggest = (1 << 63) - 1

    bolt.run(
        f"UNWIND $rows AS row MERGE (n {{id: row.vertex}}) SET n:{label}, n.big = row.big",
        {"rows": [{"vertex": biggest, "big": biggest}]},
    )
    got = bolt.run(f"MATCH (n:{label} {{id: $id}}) RETURN n.big AS big", {"id": biggest})
    assert got == [{"big": biggest}]


def test_list_properties_are_rejected(bolt, run_scope):
    """Values-as-nodes is forced by the engine, not a stylistic choice."""
    label = run_scope.label("ListProp")
    with pytest.raises(Neo4jError) as excinfo:
        bolt.run(
            f"UNWIND $rows AS row MERGE (n {{id: row.vertex}}) SET n:{label}, n.tags = row.tags",
            {"rows": [{"vertex": run_scope.node_id(label, "x"), "tags": ["a", "b"]}]},
        )
    assert "scalar" in str(excinfo.value).lower()


def test_unwind_batch_admission_limit_is_1024(bolt, run_scope):
    """Admission control caps a batch; the loader refuses a larger size up front."""
    label = run_scope.label("Admission")
    rows = [
        {"vertex": run_scope.node_id(label, f"n{i}"), "name": "x"} for i in range(1100)
    ]
    with pytest.raises(Neo4jError) as excinfo:
        bolt.run(
            f"UNWIND $rows AS row MERGE (n {{id: row.vertex}}) SET n:{label}, n.name = row.name",
            {"rows": rows},
        )
    assert "admission control" in str(excinfo.value).lower()


# --- Read forms -------------------------------------------------------------


def test_in_predicate_is_rejected_and_or_chain_is_the_alternative(
    bolt, run_scope, upsert_nodes
):
    """There is no batch multi-id read; the OR chain is what is left."""
    label = run_scope.label("MultiRead")
    ids = [run_scope.node_id(label, f"d{i}") for i in range(3)]
    upsert_nodes(label, [{"vertex": i, "name": f"n{n}"} for n, i in enumerate(ids)])

    with pytest.raises(Neo4jError):
        bolt.run(f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id AS id", {"ids": ids})

    rows = bolt.run(
        f"MATCH (n:{label}) WHERE n.id = $id0 OR n.id = $id1 RETURN n.id AS id",
        {"id0": ids[0], "id1": ids[1]},
    )
    assert {r["id"] for r in rows} == {ids[0], ids[1]}


def test_unwind_multi_id_read_is_rejected(bolt, run_scope):
    """The obvious replacement for IN does not exist either.

    Pinned so nobody reintroduces it: UNWIND batches are for one-hop
    relationship patterns, not for fanning out single-node lookups.
    """
    label = run_scope.label("UnwindRead")
    with pytest.raises(Neo4jError) as excinfo:
        bolt.run(
            f"UNWIND $rows AS row MATCH (n:{label} {{id: row.id}}) RETURN n.id AS id",
            {"rows": [{"id": run_scope.node_id(label, "a")}]},
        )
    assert "one-hop" in str(excinfo.value).lower()


def test_variable_length_paths_must_be_bounded(bolt, run_scope, upsert_nodes):
    label = run_scope.label("Hop")
    a, b, c = (run_scope.node_id(label, k) for k in "abc")
    upsert_nodes(label, [{"vertex": v, "name": k} for k, v in zip("abc", (a, b, c))])
    for src, dst in ((a, b), (b, c)):
        bolt.run(
            "UNWIND $rows AS row "
            f"MATCH (s:{label} {{id: row.src}}), (d:{label} {{id: row.dst}}) "
            "MERGE (s)-[r:CHAIN {id: row.eid}]->(d)",
            {"rows": [{"src": src, "dst": dst, "eid": run_scope.edge_id("CHAIN", src, dst)}]},
        )

    reachable = bolt.run(
        f"MATCH (s:{label} {{id: $id}})-[:CHAIN*1..2]->(v) RETURN v.id AS id", {"id": a}
    )
    assert {r["id"] for r in reachable} == {b, c}

    with pytest.raises(Neo4jError):
        bolt.run(f"MATCH (s:{label} {{id: $id}})-[:CHAIN*]->(v) RETURN v.id AS id", {"id": a})


def test_two_statements_per_request_are_rejected(bolt):
    with pytest.raises(Neo4jError) as excinfo:
        bolt.run("RETURN 1 AS a; RETURN 2 AS b")
    assert "one cypher statement" in str(excinfo.value).lower()


# --- Native path procedures -------------------------------------------------


def test_sppaths_returns_a_renderable_path(bolt, run_scope, upsert_nodes):
    """The UI renders the path structure directly, so its shape is a contract."""
    label = run_scope.label("PathNode")
    src = run_scope.node_id(label, "person")
    dst = run_scope.node_id(label, "document")
    upsert_nodes(label, [{"vertex": src, "name": "sam"}, {"vertex": dst, "name": "doc"}])
    bolt.run(
        "UNWIND $rows AS row "
        f"MATCH (s:{label} {{id: row.src}}), (d:{label} {{id: row.dst}}) "
        "MERGE (s)-[r:EVIDENCES {id: row.eid}]->(d)",
        {"rows": [{"src": src, "dst": dst, "eid": run_scope.edge_id("EVIDENCES", src, dst)}]},
    )

    rows = bolt.run(
        "CALL algo.SPpaths({sourceNode: $src, targetNode: $dst, "
        "relTypes: ['EVIDENCES'], maxLen: 3, relDirection: 'both', pathCount: 5}) "
        "YIELD path RETURN path",
        {"src": src, "dst": dst},
    )

    assert rows, "no path between two directly connected nodes"
    path = rows[0]["path"]
    # Alternating node / relationship-type / node.
    assert len(path) >= 3
    assert path[1] == "EVIDENCES"


def test_mspaths_resolves_many_sources_in_one_call(bolt, run_scope, upsert_nodes):
    """The native fan-out replacement for a client-side loop over sources."""
    label = run_scope.label("MultiSource")
    people = {name: run_scope.node_id(label, name) for name in ("sam", "soham")}
    target = run_scope.node_id(label, "shared-doc")
    upsert_nodes(
        label,
        [{"vertex": v, "name": k} for k, v in people.items()]
        + [{"vertex": target, "name": "shared-doc"}],
    )
    for name, src in people.items():
        bolt.run(
            "UNWIND $rows AS row "
            f"MATCH (s:{label} {{id: row.src}}), (d:{label} {{id: row.dst}}) "
            "MERGE (s)-[r:MENTIONS {id: row.eid}]->(d)",
            {"rows": [{"src": src, "dst": target,
                       "eid": run_scope.edge_id("MENTIONS", src, target)}]},
        )

    # sourceLabel and targetLabel must be string *literals* — a parameter is
    # rejected with "sourceLabel must be a string literal". The label is
    # interpolated, which is safe only because it comes from the run scope and
    # matches the identifier pattern; caller-supplied labels are validated by
    # hydra_client.check_identifier before they ever reach a statement.
    rows = bolt.run(
        f"CALL algo.MSpaths({{sourceLabel: '{label}', sourceProperty: 'name', "
        "sourceValues: ['sam', 'soham'], "
        f"targetLabel: '{label}', targetProperty: 'name', "
        "targetValues: ['shared-doc'], relTypes: ['MENTIONS'], relDirection: 'both', "
        "maxLen: 3, pathCount: 5, resultLimit: 50}) YIELD path RETURN path"
    )
    assert len(rows) >= 2, "MSpaths did not resolve both source values"


# --- Measurement ------------------------------------------------------------


@pytest.mark.benchmark
def test_ingest_throughput(bolt, run_scope, capsys):
    """Not an assertion — a number for the ingestion budget."""
    label = run_scope.label("Throughput")
    rows = [
        {"vertex": run_scope.node_id(label, f"t{i}"), "name": "x"} for i in range(1000)
    ]
    started = time.perf_counter()
    bolt.run(
        f"UNWIND $rows AS row MERGE (n {{id: row.vertex}}) SET n:{label}, n.name = row.name",
        {"rows": rows},
    )
    elapsed = time.perf_counter() - started
    with capsys.disabled():
        print(f"\n  ingest: {len(rows) / elapsed:,.0f} rows/s "
              f"({len(rows)} rows in {elapsed:.3f}s)")


# --- Revising a decision ------------------------------------------------------


def test_relationship_delete_requires_anonymous_endpoints(bolt, run_scope, upsert_nodes):
    """Deleting an edge inverts the naming rule the rest of this suite pins.

    Everywhere else a node carrying a label must be named. Relationship deletion
    through `UNWIND` demands the opposite, and getting it wrong is a syntax
    error rather than a silent no-op — so the working form is pinned here.

    This is what makes an identity decision revisable:
    `scripts/37_rebuild_resolution.py` removes superseded `RESOLVES_TO` edges
    exactly this way.
    """
    label = run_scope.label("DelNode")
    src = run_scope.node_id(label, "src")
    dst = run_scope.node_id(label, "dst")
    eid = run_scope.edge_id("SUPERSEDED", src, dst)
    upsert_nodes(label, [{"vertex": src, "name": "a"}, {"vertex": dst, "name": "b"}])
    bolt.run(
        "UNWIND $rows AS row "
        f"MATCH (s:{label} {{id: row.src}}), (d:{label} {{id: row.dst}}) "
        "MERGE (s)-[r:SUPERSEDED {id: row.eid}]->(d)",
        {"rows": [{"src": src, "dst": dst, "eid": eid}]},
    )
    count = f"MATCH (s:{label})-[r:SUPERSEDED]->(d:{label}) RETURN count(*) AS n"
    assert bolt.run(count)[0]["n"] == 1

    # Named endpoints are rejected outright.
    with pytest.raises(Neo4jError):
        bolt.run(
            "UNWIND $rows AS row "
            f"MATCH (s:{label} {{id: row.src}})-[e:SUPERSEDED {{id: row.eid}}]->"
            f"(d:{label} {{id: row.dst}}) DELETE e",
            {"rows": [{"src": src, "dst": dst, "eid": eid}]},
        )

    # Anonymous endpoints, with the id in the relationship pattern, work.
    bolt.run(
        "UNWIND $rows AS row MATCH ()-[e:SUPERSEDED {id: row.eid}]->() DELETE e",
        {"rows": [{"eid": eid}]},
    )
    assert bolt.run(count)[0]["n"] == 0, "the edge survived its own deletion"


def test_a_relationship_id_cannot_be_read_back(bolt, run_scope, upsert_nodes):
    """`RETURN r.id` is rejected, while every other property on it returns.

    So an edge cannot be found by reading its id out of the graph — which is why
    edge ids are derived deterministically from the endpoints instead.
    """
    label = run_scope.label("RelIdNode")
    src = run_scope.node_id(label, "src")
    dst = run_scope.node_id(label, "dst")
    eid = run_scope.edge_id("TAGGED", src, dst)
    upsert_nodes(label, [{"vertex": src, "name": "a"}, {"vertex": dst, "name": "b"}])
    bolt.run(
        "UNWIND $rows AS row "
        f"MATCH (s:{label} {{id: row.src}}), (d:{label} {{id: row.dst}}) "
        # The value has to come off the row map; a literal is rejected with
        # `UNWIND relationship SET values must read from the row map`.
        "MERGE (s)-[r:TAGGED {id: row.eid}]->(d) SET r.method = row.method",
        {"rows": [{"src": src, "dst": dst, "eid": eid, "method": "probe"}]},
    )

    rows = bolt.run(
        f"MATCH (s:{label})-[r:TAGGED]->(d:{label}) RETURN r.method AS v")
    assert rows[0]["v"] == "probe", "an ordinary relationship property must read"

    with pytest.raises(Neo4jError):
        bolt.run(f"MATCH (s:{label})-[r:TAGGED]->(d:{label}) RETURN r.id AS v")
