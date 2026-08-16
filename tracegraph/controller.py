"""The deterministic answer controller.

The model writes prose; it does not decide what is true, what is cited, or
whether the question can be answered at all. Those are the controller's, and
each is settled by a check that can fail:

* Evidence comes from the graph, anchored on documents retrieval surfaced.
* Every citation is checked against the graph before it is returned, using a
  labelled match — a bare id lookup answers yes for ids that were never written.
* Every quoted span is checked against the document body it claims to come
  from, verbatim.
* An answer with no surviving evidence is an abstention, not a guess.

The returned object is PLAN.md's answer contract, including a `hydradb_trace`
carrying the queries that ran and the real read epoch and bookmark the engine
reported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from . import fts
from .hydra_client import HydraClient, parse_bookmark
from .llm import Evidence, synthesise_answer

SUPPORTED = "supported"
INSUFFICIENT = "insufficient"
CONFLICTING = "conflicting"


@dataclass
class TracedQuery:
    operation: str
    cypher: str
    hops: int
    results: int
    ms: float


@dataclass
class ControllerResult:
    answer: str
    document_ids: list[str]
    answerability: str
    confidence: float
    claims: list[dict] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    rejected_citations: list[str] = field(default_factory=list)
    rejected_spans: list[dict] = field(default_factory=list)
    trace: dict = field(default_factory=dict)

    def to_contract(self) -> dict:
        return {
            "answer": self.answer,
            "document_ids": self.document_ids,
            "answerability": self.answerability,
            "confidence": self.confidence,
            "claims": self.claims,
            "alternatives": self.alternatives,
            "hydradb_trace": self.trace,
        }


class AnswerController:
    """Turns a question into a grounded answer or an abstention."""

    def __init__(self, client: HydraClient, run_id: str, *, max_documents: int = 8) -> None:
        self.client = client
        self.run_id = run_id
        self.max_documents = max_documents
        self._queries: list[TracedQuery] = []

    # --- graph access, every call traced ------------------------------------

    def _run(self, operation: str, cypher: str, params: dict, hops: int = 1) -> list[dict]:
        started = time.perf_counter()
        rows = self.client.bolt_read(cypher, params)
        self._queries.append(TracedQuery(
            operation=operation, cypher=" ".join(cypher.split()),
            hops=hops, results=len(rows),
            ms=round((time.perf_counter() - started) * 1000, 2),
        ))
        return rows

    def retrieve_documents(self, question: str) -> list[dict]:
        """Find candidate documents — the entry points, not the answer.

        Lexical search over document bodies does this job far better than
        matching claim text: the engine has no CONTAINS, so a graph-side search
        can only match a claim subject from its stem, and a question phrased
        even slightly differently from the extracted claim finds nothing. The
        division is PLAN.md's — search finds where to look, the graph works out
        what connects.

        The index rowid is the document's graph id, so a hit is already a node.
        """
        started = time.perf_counter()
        hits = fts.search(question, limit=self.max_documents * 3)
        self._queries.append(TracedQuery(
            operation="retrieve_candidates(fts)",
            cypher=f"fts5 MATCH {fts.sanitise_query(question)[:80]}",
            hops=0, results=len(hits),
            ms=round((time.perf_counter() - started) * 1000, 2),
        ))
        if not hits:
            return []

        # Resolve ids to dsids in one anchored read rather than one per hit.
        ordered = [node_id for node_id, _ in hits]
        rows = self._run(
            "resolve_candidates",
            "MATCH (d:Document) WHERE d.run_id = $r AND (" +
            " OR ".join(f"d.id = $i{n}" for n in range(len(ordered))) +
            ") RETURN d.dsid AS dsid, d.id AS id",
            {"r": self.run_id, **{f"i{n}": i for n, i in enumerate(ordered)}},
        )
        rank = {node_id: position for position, node_id in enumerate(ordered)}
        candidates = sorted(rows, key=lambda row: rank.get(row["id"], 1 << 30))
        return candidates[: self.max_documents]

    def claims_for(self, dsid: str) -> list[dict]:
        """Claims and their evidence spans for one document.

        Anchored on the document, so this is a typed adjacency walk rather than
        a scan, and it is the two-hop traversal the evidence graph renders.
        """
        return self._run(
            "evidence_for_claims",
            "MATCH (d:Document {dsid: $dsid})-[:ASSERTS]->(c:Claim)"
            "-[:SUPPORTED_BY]->(s:EvidenceSpan) "
            "WHERE d.run_id = $r "
            "RETURN c.subject AS subject, c.predicate AS predicate, "
            "c.object AS object, c.confidence AS confidence, s.quote AS quote, "
            "s.start AS start, s.end AS end, d.dsid AS dsid, d.title AS title",
            {"dsid": dsid, "r": self.run_id},
            hops=2,
        )

    # --- validation ---------------------------------------------------------

    def citation_exists(self, dsid: str) -> bool:
        """Is this dsid a document in the graph?

        The label is mandatory. A bare `{id: N}` pattern resolves an address
        without hydrating the vertex, so it answers yes for documents that were
        never written and would make this check vacuous.
        """
        rows = self._run(
            "validate_citation",
            "MATCH (d:Document {dsid: $dsid}) WHERE d.run_id = $r "
            "RETURN d.dsid AS dsid",
            {"dsid": dsid, "r": self.run_id},
        )
        return bool(rows)

    # --- the flow -----------------------------------------------------------

    def answer(self, question: str, bodies: dict[str, str] | None = None) -> ControllerResult:
        self._queries.clear()
        started = time.perf_counter()

        candidates = self.retrieve_documents(question)
        evidence: list[Evidence] = []
        claims: list[dict] = []
        rejected_spans: list[dict] = []

        for candidate in candidates:
            dsid = candidate["dsid"]
            for row in self.claims_for(dsid):
                quote = row["quote"] or ""
                # A span is only evidence if it is still verbatim in the source.
                # Re-checking here rather than trusting ingestion means a body
                # that changed underneath the graph cannot be cited.
                if bodies is not None:
                    body = bodies.get(dsid)
                    if body is None or quote not in body:
                        rejected_spans.append({"dsid": dsid, "quote": quote[:120]})
                        continue
                claims.append({
                    "dsid": dsid, "subject": row["subject"],
                    "predicate": row["predicate"], "object": row["object"],
                    "confidence": row["confidence"], "quote": quote,
                })
                evidence.append(Evidence(dsid=dsid, text=quote, title=row["title"]))

        if not evidence:
            return self._abstain(
                question, "no evidence in the graph supports this question",
                started, claims, rejected_spans)

        result = synthesise_answer(question, evidence[:40])

        # The model may cite only what it was given, and only what still exists.
        allowed = {item.dsid for item in evidence}
        cited, rejected = [], []
        for dsid in result.citations:
            if dsid in allowed and self.citation_exists(dsid):
                if dsid not in cited:
                    cited.append(dsid)
            else:
                rejected.append(dsid)

        if not result.sufficient or not cited:
            return self._abstain(
                question, "the model reported the evidence insufficient"
                if not result.sufficient else
                "no returned citation survived validation",
                started, claims, rejected_spans, rejected=rejected)

        used = [c for c in claims if c["dsid"] in cited]
        confidence = round(
            min(0.95, 0.4 + 0.1 * len(cited) + 0.05 * min(len(used), 6)), 3)

        return ControllerResult(
            answer=result.answer, document_ids=cited, answerability=SUPPORTED,
            confidence=confidence, claims=used[:20],
            rejected_citations=rejected, rejected_spans=rejected_spans,
            trace=self._trace(started),
        )

    def _abstain(self, question, reason, started, claims, rejected_spans,
                 rejected=None) -> ControllerResult:
        return ControllerResult(
            answer=f"The available evidence does not answer this question: {reason}.",
            document_ids=[], answerability=INSUFFICIENT, confidence=0.0,
            claims=claims[:10], rejected_citations=rejected or [],
            rejected_spans=rejected_spans, trace=self._trace(started),
        )

    def _trace(self, started: float) -> dict:
        # The consistency block reports what the engine returned. read_epoch and
        # the bookmark's storage sequence come off a real response; neither is
        # synthesised, and a missing one is reported as missing.
        probe = self.client.http_query("MATCH (d:Document) RETURN count(*) AS c")
        scope = parse_bookmark(probe.bookmark) if probe.bookmark else None
        return {
            "queries": [
                {"operation": q.operation, "cypher": q.cypher, "hops": q.hops,
                 "results": q.results, "latency_ms": q.ms}
                for q in self._queries
            ],
            "procedures": [],
            "hops": max((q.hops for q in self._queries), default=0),
            "consistency": {
                "transport": "bolt+http",
                "read_epoch": probe.read_epoch,
                "bookmark": probe.bookmark,
                "storage_sequence": scope.sequence if scope else None,
            },
            "query_count": len(self._queries),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def _terms(question: str) -> list[str]:
    import re
    stop = {"what", "which", "when", "where", "does", "did", "the", "and", "for",
            "with", "that", "this", "from", "who", "how", "are", "was", "were",
            "have", "has", "about", "into", "than", "then", "they", "their"}
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", question)
    return [w for w in words if w.casefold() not in stop]
