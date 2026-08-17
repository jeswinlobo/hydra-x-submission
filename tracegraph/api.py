"""HTTP API behind the Ask & Inspect interface.

Every endpoint reads the graph. Nothing is precomputed into a fixture and
nothing is cached across requests, so what the page shows is what HydraDB holds
at that moment — including the read epoch, which is there so a viewer can see
the answer and the consistency position that produced it together.

    uv run uvicorn tracegraph.api:app --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config
from .conflicts import ClaimRecord, detect_conflicts
from .controller import AnswerController
from .hydra_client import HydraClient, parse_bookmark
from .ingest import OnDemandIngestor
from .parquet_reader import iter_documents
from .parsers import normalise_content

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="TraceGraph", version="0.1.0")

_state: dict = {"client": None, "run_id": None, "bodies": {}, "ingestor": None}


def ingestor() -> OnDemandIngestor:
    """Enriches retrieved documents so any question over the corpus can be asked."""
    if _state["ingestor"] is None:
        _state["ingestor"] = OnDemandIngestor(client(), run_id())
    return _state["ingestor"]


def client() -> HydraClient:
    if _state["client"] is None:
        _state["client"] = HydraClient()
        _state["client"].verify()
    return _state["client"]


def run_id() -> str:
    if _state["run_id"] is None:
        rows = client().bolt_read(
            "MATCH (d:Document) RETURN d.run_id AS run_id ORDER BY run_id DESC LIMIT 1")
        if not rows:
            raise HTTPException(503, "no ingested run; run scripts/30_load_slice.py")
        _state["run_id"] = rows[0]["run_id"]
    return _state["run_id"]


def bodies() -> dict[str, str]:
    """Document bodies, loaded once, for re-checking spans at answer time.

    Held in memory rather than re-read per request: the slice is small, and a
    span check that is slow enough to skip is a span check that gets skipped.
    """
    if not _state["bodies"]:
        rows = client().bolt_read(
            "MATCH (d:Document) WHERE d.run_id = $r RETURN d.dsid AS dsid",
            {"r": run_id()})
        wanted = {row["dsid"] for row in rows}
        loaded = {}
        for doc in iter_documents(columns=["doc_id", "content"]):
            if doc["doc_id"] in wanted:
                loaded[doc["doc_id"]] = normalise_content(doc["content"])
                if len(loaded) == len(wanted):
                    break
        _state["bodies"] = loaded
    return _state["bodies"]


class AskRequest(BaseModel):
    question: str


@app.get("/api/status")
def status() -> dict:
    """Graph scale and the current consistency position."""
    c = client()
    counts = {}
    for label in ("Document", "Entity", "Mention", "Claim", "EvidenceSpan", "Channel"):
        rows = c.bolt_read(
            f"MATCH (n:{label}) WHERE n.run_id = $r RETURN count(*) AS n",
            {"r": run_id()})
        counts[label] = rows[0]["n"]

    edges = {}
    for rel, src, dst in (
        ("RESOLVES_TO", "Mention", "Entity"),
        ("CANDIDATE_FOR", "Mention", "Entity"),
        ("ASSERTS", "Document", "Claim"),
        ("SUPPORTED_BY", "Claim", "EvidenceSpan"),
        ("CONFLICTS_WITH", "Claim", "Claim"),
        ("PARTICIPATED_IN", "Entity", "Channel"),
    ):
        rows = c.bolt_read(
            f"MATCH (a:{src})-[e:{rel}]->(b:{dst}) WHERE e.run_id = $r "
            "RETURN count(*) AS n", {"r": run_id()})
        edges[rel] = rows[0]["n"]

    probe = c.http_query("MATCH (d:Document) RETURN count(*) AS c")
    scope = parse_bookmark(probe.bookmark) if probe.bookmark else None
    return {
        "run_id": run_id(),
        "nodes": counts,
        "edges": edges,
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
    payload["evidence_graph"] = _evidence_graph(result.document_ids or
                                                [c["dsid"] for c in result.claims[:3]])
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

    return {"resolved": multi[:limit], "unresolved": unresolved}


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
