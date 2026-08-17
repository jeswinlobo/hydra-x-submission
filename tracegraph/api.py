"""HTTP API behind the Ask & Inspect interface.

Every endpoint reads the graph. Nothing is precomputed into a fixture and
nothing is cached across requests, so what the page shows is what HydraDB holds
at that moment — including the read epoch, which is there so a viewer can see
the answer and the consistency position that produced it together.

    uv run uvicorn tracegraph.api:app --port 8000
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config
from .conflicts import ClaimRecord, detect_conflicts
from .controller import AnswerController
from .graph_resolve import GraphEvidence
from .hydra_client import HydraClient, parse_bookmark
from .ingest import OnDemandIngestor

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="TraceGraph", version="0.1.0")

_state: dict = {"client": None, "run_id": None, "bodies": {}, "ingestor": None}

# These endpoints are synchronous, so FastAPI runs each request on a worker
# thread out of a pool. Two things follow, and both used to be wrong here.
#
# First, the lazy singletons below race: two first requests arriving together
# each saw `None` and each built a client, and the loser's connection was
# dropped on the floor still open. `_init` serialises construction so exactly
# one is built.
#
# Second, whatever is shared has to survive being used from a thread other than
# the one that made it. The id registry and the row locator each hold a SQLite
# connection, which refuses cross-thread use outright — that is the
# `sqlite3.ProgrammingError` this cache produced under concurrent questions.
# Both now carry their own lock; see `RowLocator.__init__`.
#
# The lock is reentrant because `ingestor()` holds it while calling `client()`.
_init = threading.RLock()


def ingestor() -> OnDemandIngestor:
    """Enriches retrieved documents so any question over the corpus can be asked."""
    if _state["ingestor"] is None:
        with _init:
            if _state["ingestor"] is None:
                _state["ingestor"] = OnDemandIngestor(client(), run_id())
    return _state["ingestor"]


def client() -> HydraClient:
    if _state["client"] is None:
        with _init:
            if _state["client"] is None:
                created = HydraClient()
                created.verify()
                _state["client"] = created
    return _state["client"]


DEFAULT_RUN = "ondemand"


def run_id() -> str:
    """The run this session reads and writes.

    An empty graph is a starting state, not an error. Documents are enriched
    when questions reach them, so a fresh install with nothing preloaded — which
    is what `bootstrap.sh --fast` leaves — has to be able to answer its first
    question and grow from there. Refusing to start until something was
    preloaded would put the system back to only knowing a curated slice.
    """
    if _state["run_id"] is None:
        with _init:
            if _state["run_id"] is None:
                rows = client().bolt_read(
                    "MATCH (d:Document) RETURN d.run_id AS run_id "
                    "ORDER BY run_id DESC LIMIT 1")
                _state["run_id"] = rows[0]["run_id"] if rows else DEFAULT_RUN
    return _state["run_id"]


def bodies() -> dict[str, str]:
    """Document bodies, for re-checking spans at answer time.

    A cache that fills as questions are asked, not a preload. It used to stream
    the corpus on the first question to gather every body in the run — a scan of
    the whole 1.4 GB file to collect a few hundred documents, most of which that
    question had no use for.

    The controller fetches what it is missing through the ingestor's row
    locator, which is a twelve-millisecond point lookup since the corpus was
    re-chunked, and writes it back here. A question needs at most eight bodies,
    so the work is bounded by the question rather than by the corpus, and the
    second question about the same document pays nothing.
    """
    # Bounded, because this is a process-wide cache in a server that is meant to
    # stay up. Each entry is a normalised document body and a question adds up to
    # eight; left alone it grows for as long as people keep asking. Dropping the
    # oldest half is enough — the controller re-fetches in twelve milliseconds.
    cache = _state["bodies"]
    if len(cache) > MAX_CACHED_BODIES:
        with _init:
            for dsid in list(cache)[: len(cache) // 2]:
                cache.pop(dsid, None)
    return cache


# Roughly a hundred questions' worth of documents before the cache is trimmed.
MAX_CACHED_BODIES = 800


class AskRequest(BaseModel):
    question: str


NODE_LABELS = ("Document", "Entity", "Mention", "Claim", "EvidenceSpan", "Channel")

# Counting a relationship type means anchoring on a label and expanding every
# vertex under it, so the cost tracks the size of the anchor rather than the
# number of edges. The two anchored on Mention dominate everything else —
# RESOLVES_TO takes 3.9s and CANDIDATE_FOR 8.2s against 8,889 mentions, while
# CONFLICTS_WITH, anchored on Claim, takes 15ms — and they grow as the graph
# does. So they are not counted unless asked for: the status bar polls this
# endpoint, and it displays neither.
#
# Both endpoints must carry a label. `MATCH ()-[e:CANDIDATE_FOR]->()` is planned
# as a precomputed cross join and hits the 30-second query timeout; see
# docs/engine-notes.md.
CHEAP_EDGES = (
    ("ASSERTS", "Document", "Claim"),
    ("SUPPORTED_BY", "Claim", "EvidenceSpan"),
    ("CONFLICTS_WITH", "Claim", "Claim"),
    ("PARTICIPATED_IN", "Entity", "Channel"),
)
TRAVERSAL_EDGES = (
    ("RESOLVES_TO", "Mention", "Entity"),
    ("CANDIDATE_FOR", "Mention", "Entity"),
)


@app.get("/api/status")
def status(full: bool = False) -> dict:
    """Graph scale and the current consistency position.

    `full=true` adds the two resolution edge counts, which cost about twelve
    seconds between them. The default leaves them out so the page's status bar
    fills promptly; `/api/resolution` reports the same structure in a form that
    is actually readable.
    """
    c, run = client(), run_id()
    edge_specs = CHEAP_EDGES + (TRAVERSAL_EDGES if full else ())

    def count_nodes(label: str) -> tuple[str, int]:
        rows = c.bolt_read(
            f"MATCH (n:{label}) WHERE n.run_id = $r RETURN count(*) AS n", {"r": run})
        return label, rows[0]["n"]

    def count_edges(spec: tuple[str, str, str]) -> tuple[str, int]:
        rel, src, dst = spec
        rows = c.bolt_read(
            f"MATCH (a:{src})-[e:{rel}]->(b:{dst}) WHERE e.run_id = $r "
            "RETURN count(*) AS n", {"r": run})
        return rel, rows[0]["n"]

    # Concurrently, because these are independent reads and the driver opens a
    # session per call. Serially even the cheap counts added up to a visible
    # wait; together they cost about as much as the slowest one.
    with ThreadPoolExecutor(max_workers=8) as pool:
        node_rows = pool.map(count_nodes, NODE_LABELS)
        edge_rows = pool.map(count_edges, edge_specs)
        counts, edges = dict(node_rows), dict(edge_rows)

    probe = c.http_query("MATCH (d:Document) RETURN count(*) AS c")
    scope = parse_bookmark(probe.bookmark) if probe.bookmark else None
    return {
        "run_id": run,
        "nodes": counts,
        "edges": edges,
        "edges_omitted": [] if full else [rel for rel, _, _ in TRAVERSAL_EDGES],
        "consistency": {
            "read_epoch": probe.read_epoch,
            "bookmark": probe.bookmark,
            "storage_sequence": scope.sequence if scope else None,
        },
    }


@app.post("/api/ask")
def ask(request: AskRequest) -> dict:
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "question is empty")
    controller = AnswerController(client(), run_id(), ingestor=ingestor())
    result = controller.answer(question, bodies=bodies())
    payload = result.to_contract()
    payload["rejected_citations"] = result.rejected_citations
    payload["rejected_spans"] = len(result.rejected_spans)
    # Only a supported answer gets an evidence graph. Falling back to whatever
    # was retrieved drew a subgraph under an abstention, which reads as evidence
    # for an answer the system just declined to give.
    payload["evidence_graph"] = _evidence_graph(result.document_ids)
    payload["examined_documents"] = sorted({c["dsid"] for c in result.examined})
    return payload


def _evidence_graph(dsids: list[str]) -> dict:
    """The focused subgraph behind an answer — never the whole graph.

    Bounded to the cited documents and one hop of their claims and spans, which
    is what makes it readable. PLAN.md is explicit that an uncontrolled hairball
    is not evidence.
    """
    if not dsids:
        return {"nodes": [], "edges": []}
    c = client()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for dsid in dsids[:4]:
        nodes[f"doc:{dsid}"] = {"id": f"doc:{dsid}", "kind": "Document",
                                "label": dsid[:18]}
        rows = c.bolt_read(
            "MATCH (d:Document {dsid: $dsid})-[:ASSERTS]->(cl:Claim)"
            "-[:SUPPORTED_BY]->(s:EvidenceSpan) WHERE d.run_id = $r "
            "RETURN cl.id AS cid, cl.subject AS subject, cl.predicate AS predicate, "
            "cl.object AS object, s.id AS sid, s.quote AS quote LIMIT 6",
            {"dsid": dsid, "r": run_id()})
        for row in rows:
            cid, sid = f"claim:{row['cid']}", f"span:{row['sid']}"
            nodes[cid] = {"id": cid, "kind": "Claim",
                          "label": f"{row['subject'][:22]} {row['predicate'][:14]}"}
            nodes[sid] = {"id": sid, "kind": "EvidenceSpan",
                          "label": (row["quote"] or "")[:40]}
            edges.append({"source": f"doc:{dsid}", "target": cid, "type": "ASSERTS"})
            edges.append({"source": cid, "target": sid, "type": "SUPPORTED_BY"})
    return {"nodes": list(nodes.values()), "edges": edges}


@app.get("/api/resolution")
def resolution(limit: int = 12) -> dict:
    """Entities reached by several surfaces, and the surfaces left ambiguous."""
    c = client()
    resolved = c.bolt_read(
        "MATCH (m:Mention)-[r:RESOLVES_TO]->(e:Entity) WHERE r.run_id = $r "
        "RETURN e.id AS entity_id, e.name AS name, m.normalised AS surface, "
        "r.method AS method, r.confidence AS confidence, r.evidence AS evidence, "
        "r.candidates AS candidates LIMIT 4000", {"r": run_id()})

    by_entity: dict[int, dict] = {}
    for row in resolved:
        entry = by_entity.setdefault(row["entity_id"], {
            "entity_id": row["entity_id"], "name": row["name"], "surfaces": {}})
        entry["surfaces"].setdefault(row["surface"], {
            "surface": row["surface"], "method": row["method"],
            "confidence": row["confidence"], "evidence": row["evidence"]})

    multi = [
        {**entry, "surfaces": list(entry["surfaces"].values())}
        for entry in by_entity.values() if len(entry["surfaces"]) >= 2
    ]
    multi.sort(key=lambda e: -len(e["surfaces"]))

    unresolved = c.bolt_read(
        "MATCH (m:Mention) WHERE m.run_id = $r AND m.status = 'unresolved' "
        "AND m.candidates > 1 RETURN m.surface AS surface, "
        "m.candidates AS candidates, m.reason AS reason "
        "ORDER BY m.candidates DESC LIMIT 12", {"r": run_id()})

    shown = multi[:limit]
    _attach_paths(c, shown)
    return {"resolved": shown, "unresolved": unresolved}


# How many entities get a path drawn. Each is one `algo.SPpaths` call, so this
# is bounded rather than run over everything on screen.
PATH_BUDGET = 4


def _attach_paths(c: HydraClient, entities: list[dict]) -> None:
    """Ask the engine for the path connecting a person to where they were seen.

    This is a native path procedure returning a whole path, rather than the
    application walking edges and composing a sentence about them — so what the
    panel shows and what the graph holds cannot drift apart.

    A missing path is left absent rather than faked. Not every resolved identity
    has recorded participation, and an entity resolved by its address alone
    never needed one.
    """
    evidence = GraphEvidence(c, run_id())
    for entity in entities[:PATH_BUDGET]:
        channels = c.bolt_read(
            "MATCH (e:Entity)-[p:PARTICIPATED_IN]->(ch:Channel) "
            "WHERE e.id = $eid AND p.run_id = $r "
            "RETURN ch.id AS cid, ch.name AS name LIMIT 1",
            {"eid": entity["entity_id"], "r": run_id()})
        if not channels:
            continue
        try:
            path = evidence.evidence_path(entity["entity_id"], channels[0]["cid"])
        except Exception:  # noqa: BLE001 - a missing path must not fail the panel
            continue
        if path:
            steps = _path_steps(path)
            entity["path"] = {
                "channel": channels[0]["name"],
                "procedure": "algo.SPpaths",
                # Relationships traversed, not elements returned. The engine
                # hands back a flat alternating list — node, type, node — so a
                # single participation edge arrives as three elements, and
                # reporting that as "3 hops" overstates a one-hop walk by three.
                "hops": max((len(steps) - 1) // 2, 0),
                # The path itself, so the panel renders what the engine
                # returned rather than a summary of it.
                "steps": steps,
            }


def _path_steps(path) -> list[dict]:
    """Flatten an engine path into renderable steps, in order.

    `algo.SPpaths` yields `[{node props}, 'REL_TYPE', {node props}, …]`. Nodes
    arrive as their property maps rather than as typed objects, so the label has
    to come from whichever naming property is present.
    """
    steps: list[dict] = []
    for index, element in enumerate(path or []):
        if isinstance(element, str):
            steps.append({"kind": "relationship", "label": element})
        elif isinstance(element, dict):
            label = element.get("name") or element.get("key") or element.get("dsid")
            steps.append({"kind": "node", "label": str(label or f"node {index}")})
        else:
            steps.append({"kind": "node", "label": str(element)})
    return steps


@app.get("/api/conflicts")
def conflicts(limit: int = 8) -> dict:
    """Contested facts with every version and its trust breakdown."""
    c = client()
    rows = c.bolt_read(
        "MATCH (d:Document)-[:ASSERTS]->(cl:Claim)-[:SUPPORTED_BY]->(s:EvidenceSpan) "
        "WHERE cl.run_id = $r RETURN cl.id AS claim_id, cl.dsid AS dsid, "
        "d.source_type AS source_type, cl.subject AS subject, "
        "cl.predicate AS predicate, cl.object AS object, "
        "cl.confidence AS confidence, s.quote AS quote, d.timestamp AS timestamp "
        "LIMIT 8000", {"r": run_id()})
    records = [ClaimRecord(**row) for row in rows]
    timestamps = {r.dsid: r.timestamp for r in records if r.timestamp}
    order = sorted(timestamps, key=lambda d: timestamps[d])
    found, stats = detect_conflicts(records, document_order=order)
    return {"conflicts": [c.as_dict() for c in found[:limit]], "stats": stats}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.on_event("shutdown")
def shutdown() -> None:
    if _state["client"] is not None:
        _state["client"].close()
