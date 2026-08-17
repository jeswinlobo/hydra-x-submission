"""On-demand ingestion: bring a retrieved document into the graph, now.

Without this the system only answers questions about whatever slice happened to
be loaded ahead of time. Retrieval searches all 511,962 documents, so it finds
the right document for almost any question — and then the graph has nothing to
say about it, and the controller abstains on a question the corpus can plainly
answer. That is a demo, not a working system.

So the graph is grown by use. A question retrieves candidates from the whole
corpus; any candidate not yet enriched is parsed, resolved, and extracted before
the answer is composed, and it stays enriched for every later question. The
working set converges on what people actually ask about instead of on what
someone chose to preload.

This is what makes the label-index ceiling survivable rather than fatal: the
graph holds documents that have been asked about, which is a number that grows
slowly, while the lexical index carries corpus scale.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config
from .hydra_client import HydraClient
from .ids import IdRegistry, edge_identity, node_identity
from .llm import extract_claims
from .loader import upsert_edges, upsert_nodes
from .parquet_reader import RowLocator
from .parsers import normalise_content, parse_document
from .parsers.base import PERSON

DOC = "Document"
MENTION = "Mention"
ENTITY = "Entity"
CLAIM = "Claim"
SPAN = "EvidenceSpan"


@dataclass
class _Prepared:
    """Everything read and extracted for one document, before it is written.

    Splitting preparation from the write is what lets the slow half — parsing
    and the model call — run concurrently while the graph writes stay ordered.
    """

    dsid: str
    source_type: str = ""
    title: str = ""
    body: str = ""
    mentions: list = field(default_factory=list)
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


@dataclass
class IngestReport:
    dsid: str
    already_present: bool = False
    mentions: int = 0
    claims: int = 0
    spans: int = 0
    rejected_spans: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def enriched(self) -> bool:
        return self.claims > 0 or self.mentions > 0


class OnDemandIngestor:
    """Enriches documents the moment a question reaches them.

    Extraction uses the synchronous API rather than Message Batches: a batch is
    half the price but takes minutes to come back, and a person waiting on an
    answer will not wait for it. Bulk passes still use batches; this path exists
    for the question in front of you.
    """

    def __init__(self, client: HydraClient, run_id: str, *,
                 extract: bool = True, max_body: int = 8000) -> None:
        self.client = client
        self.run_id = run_id
        self.extract = extract
        self.max_body = max_body
        self.registry = IdRegistry()
        self.locator = RowLocator(config.DOCUMENTS_PARQUET, config.REGISTRY_DB)
        self._present: set[str] = set()

    def close(self) -> None:
        self.locator.close()

    def is_present(self, dsid: str) -> bool:
        """Is this document already enriched?

        A labelled match, because a bare id pattern answers yes for documents
        that were never written and would make every check pass.
        """
        if dsid in self._present:
            return True
        rows = self.client.bolt_read(
            "MATCH (d:Document {dsid: $dsid})-[:ASSERTS]->(c:Claim) "
            "RETURN c.id AS id LIMIT 1", {"dsid": dsid})
        if rows:
            self._present.add(dsid)
            return True
        return False

    def body(self, dsid: str) -> str | None:
        record = self.locator.fetch(dsid)
        if record is None:
            return None
        return normalise_content(record.get("content") or "")

    def ingest(self, dsid: str) -> IngestReport:
        """Enrich one document, read to write."""
        if self.is_present(dsid):
            return IngestReport(dsid, already_present=True)
        return self._commit(self._prepare(dsid))

    def _prepare(self, dsid: str) -> _Prepared:
        """Read and extract. No graph writes, so this is safe to run in parallel."""
        started = time.perf_counter()
        record = self.locator.fetch(dsid)
        if record is None:
            return _Prepared(dsid, error="not in corpus",
                             seconds=time.perf_counter() - started)

        body = normalise_content(record.get("content") or "")
        prepared = _Prepared(
            dsid=dsid, source_type=record.get("source_type") or "",
            title=record.get("title") or "", body=body,
        )
        parsed = parse_document(dsid, prepared.source_type, prepared.title,
                                record.get("content") or "")
        prepared.mentions = parsed.verified_mentions(body)[:400]

        if self.extract and body.strip():
            try:
                result = extract_claims(body[: self.max_body], dsid)
                prepared.accepted = result.accepted
                prepared.rejected = result.rejected
            except Exception as exc:  # noqa: BLE001 - one document must not fail a query
                prepared.error = f"extraction failed: {exc}"[:200]
        prepared.seconds = time.perf_counter() - started
        return prepared

    def _commit(self, prepared: _Prepared) -> IngestReport:
        """Write what was prepared. Serialised, because the graph writes are."""
        started = time.perf_counter()
        dsid = prepared.dsid
        if prepared.error and not prepared.accepted and not prepared.mentions:
            return IngestReport(dsid, error=prepared.error,
                                seconds=prepared.seconds)

        body, title = prepared.body, prepared.title
        source_type = prepared.source_type
        report = IngestReport(dsid, error=prepared.error)

        pending = []
        doc_identity = node_identity(DOC, dsid)
        pending.append(doc_identity)
        upsert_nodes(self.client, DOC, [{
            "vertex": doc_identity.id, "dsid": dsid, "source_type": source_type,
            "title": title[:500], "run_id": self.run_id,
        }], job=f"ondemand:{dsid}",
            properties=["dsid", "source_type", "title", "run_id"])

        # --- structure -------------------------------------------------------
        mention_rows, mentioned_in = [], []
        for mention in prepared.mentions:
            identity = node_identity(
                MENTION, f"{dsid}:{mention.start}:{mention.end}")
            pending.append(identity)
            mention_rows.append({
                "vertex": identity.id, "surface": mention.surface[:300],
                "normalised": mention.surface.casefold()[:300],
                "kind": mention.kind, "role": mention.role,
                "start": mention.start, "end": mention.end,
                "dsid": dsid, "run_id": self.run_id,
                "status": "pending", "method": "", "candidates": 0, "reason": "",
            })
            edge = edge_identity("MENTIONED_IN", identity.id, doc_identity.id)
            pending.append(edge)
            mentioned_in.append({
                "src": identity.id, "dst": doc_identity.id, "eid": edge.id,
                "role": mention.role, "run_id": self.run_id,
            })

        if mention_rows:
            upsert_nodes(self.client, MENTION, mention_rows,
                         job=f"ondemand-m:{dsid}",
                         properties=["surface", "normalised", "kind", "role",
                                     "start", "end", "dsid", "run_id",
                                     "status", "method", "candidates", "reason"])
            upsert_edges(self.client, "MENTIONED_IN", mentioned_in,
                         job=f"ondemand-mi:{dsid}", source_label=MENTION,
                         target_label=DOC, properties=["role", "run_id"])
        report.mentions = len(mention_rows)

        # --- claims ----------------------------------------------------------
        if prepared.accepted:
            result = prepared
            report.rejected_spans = len(prepared.rejected)
            claim_rows, span_rows, asserts, supported = [], [], [], {}
            seen_spans: dict[str, int] = {}
            for claim in result.accepted:
                key = (f"{dsid}|{claim.subject}|{claim.predicate}"
                       f"|{claim.object}|{claim.span_start}")
                identity = node_identity(CLAIM, key)
                pending.append(identity)
                claim_rows.append({
                    "vertex": identity.id, "dsid": dsid,
                    "subject": claim.subject[:200],
                    "predicate": claim.predicate[:120],
                    "object": claim.object[:200],
                    "object_type": claim.object_type,
                    "confidence": float(claim.confidence), "run_id": self.run_id,
                })
                span_key = f"{dsid}:{claim.span_start}:{claim.span_end}"
                if span_key not in seen_spans:
                    span_identity = node_identity(SPAN, span_key)
                    pending.append(span_identity)
                    seen_spans[span_key] = span_identity.id
                    span_rows.append({
                        "vertex": span_identity.id, "dsid": dsid,
                        "start": int(claim.span_start), "end": int(claim.span_end),
                        "quote": claim.evidence_span[:900], "run_id": self.run_id,
                    })
                a = edge_identity("ASSERTS", doc_identity.id, identity.id)
                pending.append(a)
                asserts.append({"src": doc_identity.id, "dst": identity.id,
                                "eid": a.id, "run_id": self.run_id})
                s = edge_identity("SUPPORTED_BY", identity.id, seen_spans[span_key])
                pending.append(s)
                supported[(identity.id, seen_spans[span_key])] = {
                    "src": identity.id, "dst": seen_spans[span_key],
                    "eid": s.id, "run_id": self.run_id,
                }

            if claim_rows:
                upsert_nodes(self.client, CLAIM, claim_rows, job=f"ondemand-c:{dsid}",
                             properties=["dsid", "subject", "predicate", "object",
                                         "object_type", "confidence", "run_id"])
                upsert_nodes(self.client, SPAN, span_rows, job=f"ondemand-s:{dsid}",
                             properties=["dsid", "start", "end", "quote", "run_id"])
                upsert_edges(self.client, "ASSERTS", asserts,
                             job=f"ondemand-a:{dsid}", source_label=DOC,
                             target_label=CLAIM, properties=["run_id"])
                upsert_edges(self.client, "SUPPORTED_BY", list(supported.values()),
                             job=f"ondemand-sb:{dsid}", source_label=CLAIM,
                             target_label=SPAN, properties=["run_id"])
            report.claims = len(claim_rows)
            report.spans = len(span_rows)

        self.registry.register_many(pending)
        self._present.add(dsid)
        report.seconds = prepared.seconds + (time.perf_counter() - started)
        return report

    def ingest_many(self, dsids: list[str], *, budget: int = 6) -> list[IngestReport]:
        """Enrich up to `budget` documents that are not already present.

        Bounded because each new document costs a model call, and a question
        does not need every candidate enriched to be answered — only enough of
        them.

        The extractions run concurrently. Done one after another they dominate
        the response: four documents took around eighty seconds, which is not a
        question-answering system anybody would wait for. They are independent
        calls to a remote API, so the wall time is that of the slowest rather
        than the sum.
        """
        todo = [d for d in dsids if not self.is_present(d)][:budget]
        present = [IngestReport(d, already_present=True)
                   for d in dsids if self.is_present(d)]
        if not todo:
            return present

        # Extraction is the slow half and is thread-safe; the graph writes that
        # follow are serialised by the driver.
        results: dict[str, IngestReport] = {}
        with ThreadPoolExecutor(max_workers=min(len(todo), 6)) as pool:
            futures = {pool.submit(self._prepare, d): d for d in todo}
            for future in as_completed(futures):
                dsid = futures[future]
                try:
                    results[dsid] = future.result()
                except Exception as exc:  # noqa: BLE001 - one document must not fail a query
                    results[dsid] = _Prepared(
                        dsid, error=f"{type(exc).__name__}: {exc}"[:200])

        reports = list(present)
        for dsid in todo:
            prepared = results.get(dsid)
            reports.append(self._commit(prepared) if prepared is not None
                           else IngestReport(dsid, error="no result"))
        return reports
