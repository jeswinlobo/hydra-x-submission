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

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config
from .graph_resolve import GraphEvidence
from .hydra_client import HydraClient
from .ids import IdRegistry, edge_identity, node_identity
from .llm import extract_claims
from .loader import upsert_edges, upsert_nodes
from .parquet_reader import RowLocator
from .parsers import normalise_content, parse_document
from .parsers.base import PERSON, name_tokens
from .reconcile import reconcile_conflicts
from .resolve import (CONFIDENCE, METHOD_GRAPH_EVIDENCE, METHOD_GRAPH_PROPOSED,
                      METHOD_UNRESOLVED, Resolver, pack)

DOC = "Document"
MENTION = "Mention"
ENTITY = "Entity"
CLAIM = "Claim"
SPAN = "EvidenceSpan"
CHANNEL = "Channel"
TICKET = "Ticket"

# `TICKET_KEY_RE` is deliberately loose — `[A-Z]{2,10}-\d{1,6}` — because ticket
# schemes are per-company and guessing the prefix set would miss most of them.
# That looseness catches a predictable family of things that are not tickets:
# cipher and digest sizes, severity labels, standards, and key lengths. They are
# excluded by prefix rather than by pattern because the *shape* is genuinely
# identical — `AES-256` and `PROJ-256` cannot be told apart without knowing what
# `AES` is.
_NOT_A_TICKET_PREFIX = frozenset({
    "AES", "SHA", "MD", "RSA", "ECDSA", "HMAC", "SSL", "TLS", "SSH", "GPG",
    "UTF", "ISO", "RFC", "ASCII", "HTTP", "HTTPS", "IPV", "IEEE", "ANSI",
    "SOC", "PCI", "HIPAA", "GDPR", "FIPS", "NIST", "CVE", "CWE", "CVSS",
    "SEV", "P", "SLA", "SLO", "SLI", "TTL", "QPS", "RPS", "GPU", "CPU",
    "RAM", "SSD", "NVME", "PCIE", "DDR", "INT", "FP", "BF", "FP16", "INT8",
    "USD", "EUR", "GBP", "UTC", "GMT", "AM", "PM",
})

# A four-digit tail in a plausible year range is almost always a date rather
# than a ticket number (`INC-2026`, `Q4-2025`). Real ticket counters do reach
# four digits, so this only fires when the prefix is not already known to be a
# ticket scheme in this corpus — see `_ticket_keys`.
_YEAR_LIKE = range(1990, 2101)


def _ticket_keys(references) -> list[str]:
    """The ticket keys in `references` that are plausibly real tickets.

    Returns canonical upper-case keys, deduplicated, order preserved. A document
    citing the same ticket five times is one edge, not five: the edge means
    "this document refers to that ticket", and repetition does not make it truer.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for ref in references:
        if ref.kind != "ticket":
            continue
        key = ref.target.upper()
        prefix, _, number = key.partition("-")
        if prefix in _NOT_A_TICKET_PREFIX:
            continue
        if len(number) == 4 and number.isdigit() and int(number) in _YEAR_LIKE:
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


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
    channel: str | None = None
    mentions: list = field(default_factory=list)
    references: list = field(default_factory=list)
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


@dataclass
class IngestReport:
    dsid: str
    already_present: bool = False
    mentions: int = 0
    tickets: int = 0
    resolved: int = 0
    unresolved: int = 0
    claims: int = 0
    spans: int = 0
    conflicts: int = 0
    conflict_edges: int = 0
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
                 extract: bool = True, max_body: int = 16000,
                 resolve: bool = True, reconcile: bool = True) -> None:
        self.client = client
        self.run_id = run_id
        self.extract = extract
        self.resolve = resolve
        # Whether newly written claims re-adjudicate the facts they touch. Off
        # only for tests that assert on claim writes in isolation.
        self.reconcile = reconcile
        self.max_body = max_body
        self.registry = IdRegistry()
        self.locator = RowLocator(config.locator_parquet(), config.locator_db(),
                                  require_complete=True)
        self._present: set[str] = set()
        self._resolver: Resolver | None = None
        self._adopted: set[str] = set()
        self._lock = threading.RLock()

    def close(self) -> None:
        self.locator.close()

    # --- identity ------------------------------------------------------------

    def resolver(self) -> Resolver:
        """A resolver carrying every person the graph already knows.

        Built once and then kept current, because the candidate pool is what
        makes single-document resolution work at all: a resolver that has seen
        only the document in front of it cannot match `sam` to anybody, since
        the person called Sam was established by some other document entirely.
        """
        if self._resolver is None:
            # Built into a local and published only once adoption succeeds. It
            # used to be assigned first, so a single Bolt blip during the first
            # question left an empty resolver cached for the life of the
            # process — every bare handle unresolved from then on, silently,
            # because `_adopt_known` has no other caller.
            resolver = Resolver()
            self._adopt_known(resolver)
            self._resolver = resolver
        return self._resolver

    def superseded_keys(self) -> dict[str, str]:
        """Identities folded into another, mapped to the one that survived.

        A merge writes `MERGED_INTO` rather than deleting the loser, because
        deleting a vertex would strand whatever still references it. That means
        the loser is still in the graph, and adopting it would undo the merge on
        every restart: it comes back as its own protected identity, and two
        protected identities are never merged, so `Camila Reyes` returned to six
        candidates the moment the process was restarted.
        """
        return {row["from"]: row["to"] for row in self.client.bolt_read(
            "MATCH (a:Entity)-[m:MERGED_INTO]->(b:Entity) WHERE m.run_id = $r "
            "RETURN a.key AS from, b.key AS to LIMIT 20000",
            {"r": self.run_id})}

    def _adopt_known(self, resolver: Resolver) -> int:
        """Pull entities out of the graph and into the resolver.

        Bounded, and deliberately so — this is the candidate pool for one
        document's surfaces, not a mirror of the graph. Entities arrive in a
        stable order so the pool does not shift underneath a repeated question.

        Superseded identities are skipped. Without that the candidate pool grows
        back to the pre-merge population on every restart, and canonicalisation
        holds only until the process dies.
        """
        superseded = self.superseded_keys()
        rows = self.client.bolt_read(
            "MATCH (e:Entity) WHERE e.run_id = $r "
            "RETURN e.key AS key, e.name AS name, e.emails AS emails, "
            "e.domains AS domains ORDER BY e.key LIMIT 4000",
            {"r": self.run_id})
        adopted = 0
        for row in rows:
            key = row["key"]
            if not key or key in self._adopted or key in superseded:
                continue
            resolver.adopt(
                key, row["name"] or key,
                # `emails` is a truncated join, so its last element may be half
                # an address. Reading a fragment back as real would mint a
                # permanent fake address and widen this person's token set;
                # `grace_oco` got into the graph exactly that way. The key
                # itself carries the address that named the identity, so
                # dropping anything without an `@` loses nothing that matters.
                [e for e in (row["emails"] or "").split(";") if "@" in e],
                [d for d in (row["domains"] or "").split(";") if d],
            )
            self._adopted.add(key)
            adopted += 1
        return adopted

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

    def _read(self, dsid: str) -> _Prepared:
        """Read and parse. Main thread only.

        The locator holds a SQLite connection, and SQLite objects cannot cross
        threads. Reading here rather than inside the worker is not a workaround
        for that rule so much as the right split anyway: the parquet read is
        milliseconds and the model call is seconds, so only the latter is worth
        parallelising.
        """
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
        prepared.references = parsed.references
        prepared.channel = parsed.attributes.get("channel")
        prepared.seconds = time.perf_counter() - started
        return prepared

    def _extract(self, prepared: _Prepared) -> _Prepared:
        """Call the model. Safe to run in parallel — it touches nothing local."""
        if not self.extract or not prepared.body.strip() or prepared.error:
            return prepared
        started = time.perf_counter()
        try:
            # 8,000 characters truncated 35.7% of the corpus and discarded
            # 11.1% of all text, concentrated in exactly the long documents that
            # multi-document questions depend on. 16,000 retains 99.5% for about
            # twice the input tokens on a third of documents — Haiku input is
            # the cheapest thing in this pipeline, and evidence the extractor
            # never saw cannot be cited, contested, or abstained over.
            result = extract_claims(prepared.body[: self.max_body], prepared.dsid)
            prepared.accepted = result.accepted
            prepared.rejected = result.rejected
        except Exception as exc:  # noqa: BLE001 - one document must not fail a query
            prepared.error = f"extraction failed: {exc}"[:200]
        prepared.seconds += time.perf_counter() - started
        return prepared

    def _prepare(self, dsid: str) -> _Prepared:
        return self._extract(self._read(dsid))

    def _commit(self, prepared: _Prepared) -> IngestReport:
        """Write what was prepared.

        Serialised on purpose. The API shares one ingestor across its worker
        threads, and the resolver, the adopted-key set and the id registry are
        all mutated here; two questions committing at once would interleave
        their identity decisions. Writing is the cheap half — the model call it
        follows already ran in parallel — so there is nothing to gain by
        overlapping it and a resolution to lose.
        """
        with self._lock:
            return self._commit_locked(prepared)

    def _commit_locked(self, prepared: _Prepared) -> IngestReport:
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
        mention_ids: dict[tuple[int, int], int] = {}
        for mention in prepared.mentions:
            identity = node_identity(
                MENTION, f"{dsid}:{mention.start}:{mention.end}")
            pending.append(identity)
            mention_ids[(mention.start, mention.end)] = identity.id
            mention_rows.append({
                "vertex": identity.id, "surface": mention.surface[:300],
                "normalised": mention.surface.casefold()[:300],
                "kind": mention.kind, "role": mention.role,
                "start": mention.start, "end": mention.end,
                "dsid": dsid, "run_id": self.run_id,
                # Written pending and overwritten by the resolution pass below.
                # A mention that stays pending is a bug, not a state: every one
                # has to end resolved or explicitly unresolved, because an
                # absent edge cannot otherwise be told from a failed write.
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

        # --- cross-document structure ----------------------------------------
        #
        # Every parser already extracts ticket keys into `ParsedDoc.references`,
        # and until now nothing read them. They are the one exact, inference-free
        # link between documents this corpus offers: a Slack thread and a Jira
        # export that both name `PROJ-412` are talking about the same work, and
        # no amount of lexical similarity between them establishes that.
        #
        # This matters most for the six sources with no dedicated parser, which
        # fall through to `generic` and contribute no mentions at all — a ticket
        # key is the only structure recoverable from them.
        ticket_rows, references = [], []
        for key in _ticket_keys(prepared.references):
            identity = node_identity(TICKET, key)
            pending.append(identity)
            ticket_rows.append({
                "vertex": identity.id, "key": key, "run_id": self.run_id,
            })
            edge = edge_identity("REFERENCES", doc_identity.id, identity.id)
            pending.append(edge)
            references.append({
                "src": doc_identity.id, "dst": identity.id, "eid": edge.id,
                "run_id": self.run_id,
            })

        if ticket_rows:
            upsert_nodes(self.client, TICKET, ticket_rows,
                         job=f"ondemand-t:{dsid}",
                         properties=["key", "run_id"])
            upsert_edges(self.client, "REFERENCES", references,
                         job=f"ondemand-r:{dsid}", source_label=DOC,
                         target_label=TICKET, properties=["run_id"])
        report.tickets = len(ticket_rows)

        # --- identity --------------------------------------------------------
        if mention_rows and self.resolve:
            try:
                report.resolved, report.unresolved = self._resolve_mentions(
                    prepared, doc_identity.id, mention_ids, pending)
            except Exception as exc:  # noqa: BLE001 - a question must still get an answer
                report.error = (report.error or
                                f"resolution failed: {exc}"[:200])

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

    # A surface shorter than this is not a name being shortened, it is noise.
    _MIN_SHORT_FORM = 3
    # How decisively the graph must separate its own proposals. This tier has no
    # lexical corroboration, so "one candidate scored and the rest did not" is
    # the only evidence there is, and a second scoring candidate means the graph
    # cannot tell them apart — which is an abstention, not a coin toss.
    _PROPOSAL_REQUIRES_SOLE_WINNER = True

    def _propose_from_structure(
        self, evidence: GraphEvidence, mention, doc_id: int,
        channel_id: int | None, entity_ids: dict, resolver,
    ) -> tuple[int, str, float] | None:
        """Let the graph name somebody no string rule could offer.

        The track brief opens on this exact case — *"deciding that 'Sam',
        '@soham' and 'S. Ratnaparkhi' are one person"* — and `Sam` is the one of
        the three that string matching cannot reach. `{sam}` is not a subset of
        `{soham, ratnaparkhi}`; there is no shared token, no small edit
        distance, and no embedding of two four-letter strings that recovers the
        relationship. The signal is not in the text at all.

        It is in the graph: who speaks in this channel, who is already resolved
        inside this document. So the candidate set comes from structure, and the
        same co-occurrence and participation traversals that score every other
        ambiguous surface then decide between them.

        Three guards, because this is the weakest inference the resolver makes
        and a wrong answer here is a false merge — the failure this module
        exists to refuse:

        * a single token of at least three characters, so this fires on short
          forms rather than on prose;
        * a shared first initial, which every real short form has and which stops
          "one person happens to be nearby" from becoming an identity claim;
        * a sole scoring candidate. Two candidates with evidence means the graph
          cannot separate them, and the mention stays unresolved with that on
          the record.
        """
        surface = (mention.surface or "").strip()
        if mention.kind != PERSON or len(name_tokens(surface)) != 1:
            return None
        token = next(iter(name_tokens(surface)), "")
        if len(token) < self._MIN_SHORT_FORM:
            return None

        proposed = evidence.propose_from_structure(
            token[0].upper(), doc_id, channel_id)
        scoring = [p for p in proposed if (p[2] or p[3])]
        if not scoring:
            return None
        if self._PROPOSAL_REQUIRES_SOLE_WINNER and len(scoring) > 1:
            return None

        key, name, co, part = scoring[0]
        entity_id = entity_ids.get(key)
        if entity_id is None:
            person = resolver.people.get(key)
            entity_id = node_identity(ENTITY, key).id if person else None
        if entity_id is None:
            return None

        # Refuse to "resolve" a surface onto a name it already matches — that is
        # tier 2's job and would misreport which tier decided.
        if token in {t.casefold() for t in name_tokens(name)}:
            return None

        reason = (
            f"no candidate shares this surface's tokens, so the graph proposed "
            f"{name} from structure: {co} co-mention(s) in this document and "
            f"{part} shared channel(s), sole scoring candidate sharing the "
            f"initial '{token[0].upper()}'"
        )
        confidence = CONFIDENCE[METHOD_GRAPH_PROPOSED]
        return entity_id, reason, confidence

    def _resolve_mentions(self, prepared: _Prepared, doc_id: int,
                          mention_ids: dict[tuple[int, int], int],
                          pending: list) -> tuple[int, int]:
        """Decide who each mention refers to, and record the decision.

        On-demand ingestion used to stop after writing mentions, leaving every
        one of them `pending` — which is not a resolution outcome but the
        absence of one, and it meant the identity panel went blank for exactly
        the documents a question had just reached. The verification gate caught
        it as 142 pending mentions across sixteen documents.

        The tiers are the bulk loader's, in the same order and with the same
        recorded evidence: an address resolves outright, a unique token subset
        resolves by lookup, and anything still ambiguous goes to the graph,
        which separates candidates by shared context rather than by spelling. A
        surface the graph cannot separate is written `unresolved` with its
        candidate count and the reason — a recorded decision, not a gap.
        """
        resolver = self.resolver()
        dsid = prepared.dsid
        channel = prepared.channel

        # The document's own people first: an address in this document creates
        # an identity that surfaces further down the same document can match.
        resolver.observe(dsid, prepared.source_type, prepared.mentions,
                         channel=channel)
        # Every identity the graph already holds is protected from being merged
        # away. Without this the per-document merge popped persisted people:
        # their vertices stayed in the graph with their own mentions while new
        # mentions of their address resolved to whoever survived the merge — a
        # different person, at `strong_key_email` confidence 1.0.
        resolver.merge_same_person(protected=self._adopted)

        direct, ambiguous = [], []
        for mention in prepared.mentions:
            outcome = resolver.resolve_mention(
                mention, dsid, channel, use_graph_tier=False)
            (direct if outcome.resolved else ambiguous).append((mention, outcome))

        # Entities for every person now known to this document, so a
        # RESOLVES_TO edge always has a vertex to land on. The id comes from the
        # person's natural key, so a person the graph already holds is written
        # over rather than duplicated beside.
        touched = {o.person_key for _, o in direct if o.person_key}
        for _, outcome in ambiguous:
            touched.update(outcome.candidates)
        entity_ids: dict[str, int] = {}
        entity_rows = []
        for key in sorted(touched):
            person = resolver.people.get(key)
            if person is None:
                continue
            identity = node_identity(ENTITY, key)
            pending.append(identity)
            entity_ids[key] = identity.id
            entity_rows.append({
                "vertex": identity.id, "key": key, "name": person.display_name,
                "kind": PERSON,
                "emails": pack(sorted(person.emails), 400),
                "domains": pack(sorted(person.domains), 200),
                "run_id": self.run_id,
            })
        if entity_rows:
            upsert_nodes(self.client, ENTITY, entity_rows,
                         job=f"ondemand-e:{dsid}",
                         properties=["key", "name", "kind", "emails", "domains",
                                     "run_id"])
            self._adopted.update(entity_ids)

        # The channel, and the participation the graph tier reads. Participation
        # has to be written before scoring runs or the evidence is not there yet.
        channel_id = None
        if channel:
            identity = node_identity(CHANNEL, channel)
            pending.append(identity)
            channel_id = identity.id
            upsert_nodes(self.client, CHANNEL,
                         [{"vertex": channel_id, "name": channel,
                           "run_id": self.run_id}],
                         job=f"ondemand-ch:{dsid}", properties=["name", "run_id"])

        resolves, participation, statuses = [], {}, []
        for mention, outcome in direct:
            mid = mention_ids.get((mention.start, mention.end))
            target = entity_ids.get(outcome.person_key or "")
            if mid is None or target is None:
                continue
            edge = edge_identity("RESOLVES_TO", mid, target)
            pending.append(edge)
            resolves.append({
                "src": mid, "dst": target, "eid": edge.id,
                "method": outcome.method, "confidence": outcome.confidence,
                "evidence": outcome.evidence[:400],
                "candidates": len(outcome.candidates), "run_id": self.run_id,
            })
            statuses.append({
                "vertex": mid, "status": "resolved", "method": outcome.method,
                "candidates": len(outcome.candidates),
                "reason": outcome.evidence[:300],
                # Which identity, not merely that there was one. Conflict
                # adjudication reads this to tell one person from another with
                # the same name, and reads it off the mention because walking
                # RESOLVES_TO costs 7.6s against 0.5s here.
                "entity": target,
            })
            if channel_id is not None:
                key = (outcome.person_key, channel)
                if key not in participation:
                    p = edge_identity("PARTICIPATED_IN", target, channel_id)
                    pending.append(p)
                    participation[key] = {
                        "src": target, "dst": channel_id, "eid": p.id,
                        "run_id": self.run_id,
                    }

        if resolves:
            upsert_edges(self.client, "RESOLVES_TO", resolves,
                         job=f"ondemand-r:{dsid}", source_label=MENTION,
                         target_label=ENTITY,
                         properties=["method", "confidence", "evidence",
                                     "candidates", "run_id"])
        if participation:
            upsert_edges(self.client, "PARTICIPATED_IN",
                         list(participation.values()),
                         job=f"ondemand-p:{dsid}", source_label=ENTITY,
                         target_label=CHANNEL, properties=["run_id"])

        # --- the ambiguous ones, decided by the graph ------------------------
        evidence = GraphEvidence(self.client, self.run_id)
        graph_resolves, candidate_edges = [], []
        for mention, outcome in ambiguous:
            mid = mention_ids.get((mention.start, mention.end))
            if mid is None:
                continue
            candidates = {
                entity_ids[key]: resolver.people[key].display_name
                for key in outcome.candidates if key in entity_ids
            }
            if not candidates:
                # No string rule offered anybody, which is where the brief's own
                # example lives: `sam` shares no token with `soham ratnaparkhi`.
                # Every tier above has now failed by construction, so either the
                # graph proposes an identity or nothing does.
                decided = self._propose_from_structure(
                    evidence, mention, doc_id, channel_id, entity_ids, resolver)
                if decided is None:
                    statuses.append({
                        "vertex": mid, "status": "unresolved",
                        "method": METHOD_UNRESOLVED, "candidates": 0,
                        "reason": (outcome.evidence or "no candidate")[:300],
                        "entity": 0,
                    })
                    continue
                entity_id, reason, confidence = decided
                edge = edge_identity("RESOLVES_TO", mid, entity_id)
                pending.append(edge)
                graph_resolves.append({
                    "src": mid, "dst": entity_id, "eid": edge.id,
                    "method": METHOD_GRAPH_PROPOSED, "confidence": confidence,
                    "evidence": reason[:400], "candidates": 0,
                    "run_id": self.run_id,
                })
                statuses.append({
                    "vertex": mid, "status": "resolved",
                    "method": METHOD_GRAPH_PROPOSED, "candidates": 0,
                    "reason": reason[:300], "entity": entity_id,
                })
                continue

            decision = evidence.score_candidates(candidates, doc_id, channel_id)

            # Every candidate the graph weighed is recorded, so a rejected one
            # can be inspected rather than inferred from its absence.
            for entry in decision.scored[:8]:
                e = edge_identity("CANDIDATE_FOR", mid, entry.entity_id)
                pending.append(e)
                candidate_edges.append({
                    "src": mid, "dst": entry.entity_id, "eid": e.id,
                    "score": entry.score, "co_occurrences": entry.co_occurrences,
                    "participations": entry.participations,
                    "run_id": self.run_id,
                })

            if decision.winner is None:
                statuses.append({
                    "vertex": mid, "status": "unresolved",
                    "method": METHOD_UNRESOLVED, "candidates": len(candidates),
                    "reason": (decision.reason or "")[:300],
                    "entity": 0,
                })
                continue

            edge = edge_identity("RESOLVES_TO", mid, decision.winner.entity_id)
            pending.append(edge)
            confidence = round(0.5 + 0.45 * decision.margin, 3)
            graph_resolves.append({
                "src": mid, "dst": decision.winner.entity_id, "eid": edge.id,
                "method": METHOD_GRAPH_EVIDENCE, "confidence": confidence,
                "evidence": decision.reason[:400], "candidates": len(candidates),
                "run_id": self.run_id,
            })
            statuses.append({
                "vertex": mid, "status": "resolved",
                "method": METHOD_GRAPH_EVIDENCE, "candidates": len(candidates),
                "reason": decision.reason[:300],
                "entity": decision.winner.entity_id,
            })

        if graph_resolves:
            upsert_edges(self.client, "RESOLVES_TO", graph_resolves,
                         job=f"ondemand-gr:{dsid}", source_label=MENTION,
                         target_label=ENTITY,
                         properties=["method", "confidence", "evidence",
                                     "candidates", "run_id"])
        if candidate_edges:
            upsert_edges(self.client, "CANDIDATE_FOR", candidate_edges,
                         job=f"ondemand-cf:{dsid}", source_label=MENTION,
                         target_label=ENTITY,
                         properties=["score", "co_occurrences",
                                     "participations", "run_id"])

        # Statuses last, so no mention is left carrying `pending` — the state
        # that means "nobody looked", which is the one thing that must not
        # survive a completed ingest.
        if statuses:
            upsert_nodes(self.client, MENTION, statuses,
                         job=f"ondemand-st:{dsid}",
                         properties=["status", "method", "candidates", "reason",
                                     "entity"])

        resolved = sum(1 for s in statuses if s["status"] == "resolved")
        return resolved, len(statuses) - resolved

    def pending_documents(self, limit: int = 200) -> list[str]:
        """Documents holding a mention nobody decided about."""
        rows = self.client.bolt_read(
            "MATCH (m:Mention) WHERE m.run_id = $r AND m.status = 'pending' "
            "RETURN DISTINCT m.dsid AS dsid LIMIT $limit",
            {"r": self.run_id, "limit": limit})
        return [row["dsid"] for row in rows]

    def repair_pending(self, limit: int = 200) -> list[IngestReport]:
        """Resolve mentions left pending by an earlier, incomplete ingest.

        The document is re-read and its surfaces re-decided. Nothing is
        duplicated by this: mention and entity ids are derived from the document
        and the person's natural key, so a repair writes over the same vertices
        the first pass created rather than beside them.
        """
        reports = []
        for dsid in self.pending_documents(limit):
            with self._lock:
                prepared = self._read(dsid)
                report = IngestReport(dsid, error=prepared.error)
                if prepared.error or not prepared.mentions:
                    reports.append(report)
                    continue
                doc_identity = node_identity(DOC, dsid)
                mention_ids = {
                    (m.start, m.end): node_identity(
                        MENTION, f"{dsid}:{m.start}:{m.end}").id
                    for m in prepared.mentions
                }
                pending: list = []
                try:
                    report.resolved, report.unresolved = self._resolve_mentions(
                        prepared, doc_identity.id, mention_ids, pending)
                except Exception as exc:  # noqa: BLE001
                    report.error = f"resolution failed: {exc}"[:200]
                report.mentions = len(mention_ids)
                self.registry.register_many(pending)
            reports.append(report)
        return reports

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

        # Read here, extract on several threads, write back here.
        #
        # Reading cannot move into the pool: the row locator holds a SQLite
        # connection, and SQLite objects cannot cross threads. Submitting the
        # whole prepare step failed on the first fetch in every worker, which
        # left every document enriched to nothing — silently, because one
        # document failing must not fail the query. The split is right on its
        # own terms as well: the parquet read is milliseconds, the model call is
        # seconds, and only the latter is worth parallelising.
        results: dict[str, _Prepared] = {d: self._read(d) for d in todo}
        with ThreadPoolExecutor(max_workers=min(len(todo), 6)) as pool:
            futures = {pool.submit(self._extract, results[d]): d for d in todo}
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

        # Re-adjudicate once for the whole batch, after every claim is written.
        #
        # Without this step `CONFLICTS_WITH` edges existed only for documents
        # present when the bulk pass ran, so a disagreement introduced by a
        # document a question had just reached was invisible and the answer came
        # back singular and confident. Under `--fast`, where nothing is
        # preloaded, every disagreement was.
        #
        # At the batch boundary rather than per document because the read is a
        # single bulk load either way — doing it per document paid it four times
        # for one question.
        written = [r.dsid for r in reports if r.claims and not r.already_present]
        if self.reconcile and written:
            with self._lock:
                try:
                    outcome = reconcile_conflicts(
                        self.client, self.run_id, written, registry=self.registry)
                    for report in reports:
                        if report.dsid in written:
                            report.conflicts = outcome.conflicts_found
                            report.conflict_edges = outcome.edges_written
                except Exception as exc:  # noqa: BLE001 - a question must still answer
                    for report in reports:
                        report.error = report.error or f"reconcile failed: {exc}"[:200]
        return reports
