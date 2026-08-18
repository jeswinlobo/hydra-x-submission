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
from .ids import IdRegistry
from .llm import Evidence, synthesise_answer

# Disputes are read a page at a time because the filter that matters — is this
# a fact the answer actually used — runs after the read. A flat limit would cut
# before that filter.
_DISPUTE_PAGE = 60
_DISPUTE_MAX_PAGES = 20

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
    # Evidence that was looked at but did not support an answer. Kept separate
    # from `claims` so an abstention cannot be rendered as though it had support.
    examined: list[dict] = field(default_factory=list)
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
            "examined": self.examined,
            "alternatives": self.alternatives,
            "hydradb_trace": self.trace,
        }


class AnswerController:
    """Turns a question into a grounded answer or an abstention."""

    def __init__(self, client: HydraClient, run_id: str, *, max_documents: int = 8,
                 ingestor=None, ingest_budget: int = 4,
                 evidence_window: int = 40) -> None:
        self.client = client
        self.run_id = run_id
        self.max_documents = max_documents
        # Supplying an ingestor is what turns this from a reader of a preloaded
        # slice into something that answers over the whole corpus.
        self.ingestor = ingestor
        self.ingest_budget = ingest_budget
        # How much of the gathered evidence synthesis actually sees.
        #
        # This was 40 against roughly 140 claims from eight retrieved
        # documents — the model was shown under a third of what retrieval had
        # already paid for, and which third depended on document order. It also
        # made identity-seeded documents actively harmful: putting them first
        # displaced lexical evidence out of the window and cost three answers
        # that lexical retrieval alone had got right.
        self.evidence_window = evidence_window
        self.registry = IdRegistry()
        self._queries: list[TracedQuery] = []
        self._ingested: list[dict] = []

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

        # Resolve hits to dsids through the id registry, not the graph.
        #
        # Resolving through the graph would restrict candidates to documents
        # already ingested, which is precisely the trap that made this a demo:
        # retrieval searches all 511,962 documents, so most hits are documents
        # the graph has never seen, and asking the graph to name them returns
        # nothing at all. The registry holds every id ever minted, and the
        # index rowid is that id, so the mapping needs no database round trip.
        resolved: list[dict] = []
        for node, _score in hits:
            row = self.registry.lookup(node) if self.registry else None
            if row is not None and row.node_type == "Document":
                resolved.append({"dsid": row.natural_key, "id": node})
        self._queries.append(TracedQuery(
            operation="resolve_candidates(registry)",
            cypher=f"id registry lookup x{len(hits)}",
            hops=0, results=len(resolved), ms=0.0,
        ))
        return resolved[: self.max_documents]

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

    def contested(self, claims: Sequence[dict], *,
                  asserted_in: str | None = None) -> list[dict]:
        """Facts *the answer rests on* that some other document disputes.

        Two filters, both learned by over-flagging. Anchoring on cited
        *documents* was far too loose: a document about SOC 2 commitments also
        mentions people's job titles, and those being contested elsewhere marked
        the whole answer conflicting over facts it never used. Anchoring on the
        evidence claims was still too loose, for the same reason one step in —
        every claim extracted from a cited document is handed to the model, not
        just the ones it used.

        So the final test is whether the answer *states the contested value*.
        `asserted_in` is the answer prose; a version the answer never asserts is
        not a version the answer rests on. Crying wolf costs exactly what
        staying silent costs, and the demo check caught the loose form flipping
        a stable answer to `conflicting` in four rounds out of ten.

        The track brief names conflict resolution as one of the four things a
        question can need, alongside lookups, multi-hop reasoning, and knowing
        when the answer is absent. Without this step the other three were served
        and this one was not: a question about a contested fact got a confident
        answer from whichever version retrieval happened to surface, with
        nothing anywhere saying the corpus disagrees with itself. For a system
        whose entire premise is that enterprise sources contradict each other,
        that was the wrong silence.

        `CONFLICTS_WITH` is an edge, so finding the dispute is a walk from a
        claim already in hand rather than a re-comparison of everything. Both
        directions are followed: the edge is written once between a pair, and
        the used claim may sit at either end.

        The walk is anchored on the *document* in Cypher and narrowed to the
        used claims in Python. Filtering claim-by-claim in the query meant
        sixteen property-anchored `Claim` matches per answer and cost twenty
        seconds; anchoring on the document is a typed adjacency walk from a
        vertex the engine finds by property, and four of those answer the same
        question.
        """
        wanted = {(c["subject"], c["predicate"]) for c in claims}
        found: dict[tuple, dict] = {}
        # Every cited document, not the first four. An answer citing documents
        # five through eight could otherwise be called `supported` while resting
        # on a fact the graph records as disputed — a silent wrong verdict,
        # which is the one failure this whole path exists to prevent.
        for dsid in sorted({c["dsid"] for c in claims}):
            for direction, pattern in (
                ("outgoing",
                 "MATCH (d:Document {dsid: $dsid})-[:ASSERTS]->(a:Claim)"
                 "-[e:CONFLICTS_WITH]->(b:Claim) "),
                ("incoming",
                 "MATCH (d:Document {dsid: $dsid})-[:ASSERTS]->(a:Claim)"
                 "<-[e:CONFLICTS_WITH]-(b:Claim) "),
            ):
                # Paged rather than capped. Narrowing to the facts the answer
                # used happens below, in Python, so a flat limit here cuts
                # before the filter — it can discard the one dispute that
                # matters and keep sixty that do not. The document anchor keeps
                # each page a typed adjacency walk rather than a label scan.
                for page in range(_DISPUTE_MAX_PAGES):
                    rows = self._run(
                        f"contested_claims({direction})",
                        pattern +
                        "WHERE d.run_id = $r AND e.run_id = $r "
                        "RETURN a.subject AS subject, a.predicate AS predicate, "
                        "a.object AS cited_value, b.object AS rival_value, "
                        "b.dsid AS rival_dsid, e.decided AS decided, "
                        "e.margin AS margin ORDER BY b.id "
                        f"SKIP {page * _DISPUTE_PAGE} LIMIT {_DISPUTE_PAGE}",
                        {"dsid": dsid, "r": self.run_id},
                        hops=2,
                    )
                    for row in rows:
                        if (row["subject"], row["predicate"]) not in wanted:
                            continue
                        # Two documents saying the same thing corroborate; only
                        # a different value is a disagreement.
                        if row["cited_value"] == row["rival_value"]:
                            continue
                        if asserted_in is not None and not _states(
                                asserted_in, row["cited_value"]):
                            continue
                        pair = (row["subject"], row["predicate"], row["rival_value"])
                        found.setdefault(pair, {
                            "subject": row["subject"],
                            "predicate": row["predicate"],
                            "cited_value": row["cited_value"],
                            "rival_value": row["rival_value"],
                            "cited_dsid": dsid,
                            "rival_dsid": row["rival_dsid"],
                            "decided": bool(row["decided"]),
                            "margin": row["margin"],
                        })
                    if len(rows) < _DISPUTE_PAGE:
                        break
        return list(found.values())[:6]

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

        # Enrich whatever retrieval reached that the graph does not yet know
        # about. Without this the system only answers questions about a preloaded
        # slice: retrieval searches the whole corpus and finds the right
        # document, the graph has nothing to say about it, and the controller
        # abstains on a question the corpus plainly answers.
        ingested: list[dict] = []
        if self.ingestor is not None and candidates:
            for report in self.ingestor.ingest_many(
                [c["dsid"] for c in candidates], budget=self.ingest_budget
            ):
                if not report.already_present:
                    ingested.append({
                        "dsid": report.dsid, "claims": report.claims,
                        "mentions": report.mentions,
                        "rejected_spans": report.rejected_spans,
                        "seconds": round(report.seconds, 2),
                        "error": report.error,
                    })
        self._ingested = ingested

        evidence: list[Evidence] = []
        claims: list[dict] = []
        rejected_spans: list[dict] = []

        for candidate in candidates:
            dsid = candidate["dsid"]
            if bodies is not None and dsid not in bodies and self.ingestor is not None:
                # Span checking needs the body of a document enriched moments ago.
                fetched = self.ingestor.body(dsid)
                if fetched is not None:
                    bodies[dsid] = fetched
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

        result = synthesise_answer(question, evidence[: self.evidence_window])

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

        # An answer standing on a contested fact is not simply "supported". The
        # corpus disagrees with itself about it, and saying so is the whole
        # premise — the alternative is a confident answer built on whichever
        # version retrieval happened to reach first, which is the failure this
        # project exists to make visible.
        alternatives = self.contested(used, asserted_in=result.answer)
        answerability = CONFLICTING if alternatives else SUPPORTED
        if alternatives:
            # Confidence is capped rather than zeroed: the evidence is real and
            # cited, it is the agreement that is missing.
            confidence = min(confidence, 0.6)

        return ControllerResult(
            answer=result.answer, document_ids=cited, answerability=answerability,
            confidence=confidence, claims=used[:20], alternatives=alternatives,
            rejected_citations=rejected, rejected_spans=rejected_spans,
            trace=self._trace(started),
        )

    def _abstain(self, question, reason, started, claims, rejected_spans,
                 rejected=None) -> ControllerResult:
        """Refuse to answer, and carry nothing that reads as an answer.

        An abstention used to return the claims retrieval had gathered, on the
        reasoning that they were what the system looked at. But `claims` is the
        field an interface renders as "supporting claims", each with a document
        id beside it, so an abstention arrived on screen with citations under
        it — which is precisely the impression abstaining exists to avoid.

        What was examined and rejected belongs in `examined`, which no caller
        mistakes for support.
        """
        return ControllerResult(
            answer=f"The available evidence does not answer this question: {reason}.",
            document_ids=[], answerability=INSUFFICIENT, confidence=0.0,
            claims=[], examined=claims[:10], rejected_citations=rejected or [],
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
            "ingested_on_demand": self._ingested,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def _states(answer: str, value) -> bool:
    """Does the answer actually assert this value?

    Substring first, then a token-overlap fallback, because prose reformats a
    claim object: "Director, Trust & Security, Redwood Inference" may appear as
    "Director of Trust & Security". Requiring most of the distinctive tokens
    keeps that match while refusing an incidental one-word coincidence.
    """
    text = (answer or "").casefold()
    value = str(value or "").strip()
    if not value or not text:
        return False
    if value.casefold() in text:
        return True
    import re
    tokens = {t for t in re.findall(r"[a-z0-9]+", value.casefold()) if len(t) > 2}
    if len(tokens) < 2:
        return False
    present = sum(1 for t in tokens if t in text)
    return present / len(tokens) >= 0.75


def _terms(question: str) -> list[str]:
    import re
    stop = {"what", "which", "when", "where", "does", "did", "the", "and", "for",
            "with", "that", "this", "from", "who", "how", "are", "was", "were",
            "have", "has", "about", "into", "than", "then", "they", "their"}
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", question)
    return [w for w in words if w.casefold() not in stop]
