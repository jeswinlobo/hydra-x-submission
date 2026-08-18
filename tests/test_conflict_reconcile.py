"""Conflicts must appear for documents nobody preloaded.

The defect this exists to prevent: `CONFLICTS_WITH` edges were written once by
`scripts/55_conflicts.py` during bootstrap, and on-demand ingestion wrote claims
without ever re-adjudicating. The answer path walks persisted edges, so it could
only ever see disputes that existed at bootstrap. A disagreement introduced by a
document a question had just reached was invisible, and under `--fast`, where
nothing is preloaded, every disagreement was.

Nothing caught it. The stability check could not: no question *required* a
conflicting verdict, so a controller that never detected a conflict at all
passed ten rounds out of ten.

The live test below starts from claims that do not exist yet and drives the real
reconciler against a real engine, because the bug lived in the gap between
components rather than inside one. It writes under the production labels — that
is what the reconciler queries — so it scopes everything to a unique run_id and
removes it afterwards.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager

import pytest

from tracegraph.conflicts import ClaimRecord, detect_conflicts
from tracegraph.hydra_client import HydraClient
from tracegraph.ids import edge_identity, node_identity
from tracegraph.conflicts import group_key
from tracegraph.reconcile import reconcile_conflicts

SUBJECT = "Dana Okafor"

CONFLICT_COUNT = (
    "MATCH (a:Claim)-[e:CONFLICTS_WITH]->(b:Claim) WHERE e.run_id = $r "
    "RETURN count(*) AS n"
)


def record(dsid: str, obj: str, *, claim_id: int = 0, source: str = "gmail",
           subject: str = "Dana Okafor", predicate: str = "works as",
           timestamp: str = "2026-03-01") -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id, dsid=dsid, source_type=source, subject=subject,
        predicate=predicate, object=obj, confidence=0.9,
        quote=f"{subject} is {obj}", timestamp=timestamp)


# --- the adjudication itself, no database ------------------------------------

def test_two_values_for_one_single_valued_fact_is_a_conflict():
    """The unit the reconciler leans on, pinned apart from the plumbing."""
    conflicts, stats = detect_conflicts(
        [record("dsid_a", "Staff Engineer", claim_id=1),
         record("dsid_b", "Director of Platform", claim_id=2)],
        document_order=["dsid_a", "dsid_b"])
    assert stats["conflicts_found"] == 1
    assert len(conflicts[0].versions) == 2


def test_agreement_is_not_a_conflict():
    """Reconciliation runs on every ingest, so it must not invent disputes."""
    conflicts, stats = detect_conflicts(
        [record("dsid_a", "Staff Engineer", claim_id=1),
         record("dsid_b", "Staff Engineer", claim_id=2)],
        document_order=["dsid_a", "dsid_b"])
    assert stats["conflicts_found"] == 0
    assert conflicts == []


# --- the whole path, against a live engine -----------------------------------


def build_graph(client, run_id, docs_spec, *, identities=None):
    """Write documents, claims, spans — and optionally resolved identities.

    `docs_spec` is `dsid -> (raw predicate, object)`. The raw predicate is a
    parameter on purpose: the defect this file exists to catch was that
    selection compared raw spellings while adjudication compared canonical
    ones, and a fixture using one spelling everywhere cannot see it.

    `identities` is `dsid -> entity key`, which writes the Mention and Entity a
    subject resolves to. Without it every subject groups by name, which is the
    second defect: two different people sharing one.
    """
    from tracegraph.loader import upsert_edges, upsert_nodes

    ids: list[int] = []
    docs, claims, spans, asserts, supports = [], [], [], [], []
    for i, (dsid, (predicate, obj)) in enumerate(docs_spec.items()):
        did = node_identity("Document", f"{run_id}:{dsid}").id
        cid = node_identity("Claim", f"{run_id}:{dsid}:c").id
        sid = node_identity("EvidenceSpan", f"{run_id}:{dsid}:s").id
        ids += [did, cid, sid]
        docs.append({"vertex": did, "dsid": dsid, "run_id": run_id,
                     "source_type": "gmail",
                     "timestamp": f"2026-0{i + 1}-01T00:00:00"})
        claims.append({"vertex": cid, "dsid": dsid, "run_id": run_id,
                       "subject": SUBJECT, "predicate": predicate,
                       "object": obj, "confidence": 0.9})
        spans.append({"vertex": sid, "dsid": dsid, "run_id": run_id,
                      "quote": f"{SUBJECT} is {obj}"})
        asserts.append({"src": did, "dst": cid, "run_id": run_id,
                        "eid": edge_identity("ASSERTS", did, cid).id})
        supports.append({"src": cid, "dst": sid, "run_id": run_id,
                         "eid": edge_identity("SUPPORTED_BY", cid, sid).id})

    upsert_nodes(client, "Document", docs, job=f"{run_id}:d",
                 properties=["dsid", "run_id", "source_type", "timestamp"])
    upsert_nodes(client, "Claim", claims, job=f"{run_id}:c",
                 properties=["dsid", "run_id", "subject", "predicate", "object",
                             "confidence"])
    upsert_nodes(client, "EvidenceSpan", spans, job=f"{run_id}:s",
                 properties=["dsid", "run_id", "quote"])
    upsert_edges(client, "ASSERTS", asserts, job=f"{run_id}:a",
                 source_label="Document", target_label="Claim",
                 properties=["run_id"])
    upsert_edges(client, "SUPPORTED_BY", supports, job=f"{run_id}:sb",
                 source_label="Claim", target_label="EvidenceSpan",
                 properties=["run_id"])

    if identities:
        mentions, entities, resolves = [], [], []
        for dsid, key in identities.items():
            mid = node_identity("Mention", f"{run_id}:{dsid}:m").id
            eid = node_identity("Entity", f"{run_id}:{key}").id
            ids += [mid, eid]
            mentions.append({"vertex": mid, "dsid": dsid, "run_id": run_id,
                             "normalised": SUBJECT.casefold(), "surface": SUBJECT,
                             # The resolved entity is denormalised onto the
                             # mention, which is what adjudication reads; the
                             # RESOLVES_TO edge below is the same fact as a
                             # traversable edge.
                             "status": "resolved", "entity": eid})
            entities.append({"vertex": eid, "key": key, "name": SUBJECT,
                             "run_id": run_id})
            resolves.append({"src": mid, "dst": eid, "run_id": run_id,
                             "eid": edge_identity("RESOLVES_TO", mid, eid).id})
        upsert_nodes(client, "Mention", mentions, job=f"{run_id}:m",
                     properties=["dsid", "run_id", "normalised", "surface",
                                 "status", "entity"])
        upsert_nodes(client, "Entity", entities, job=f"{run_id}:e",
                     properties=["key", "name", "run_id"])
        upsert_edges(client, "RESOLVES_TO", resolves, job=f"{run_id}:rt",
                     source_label="Mention", target_label="Entity",
                     properties=["run_id"])
    return ids


@contextmanager
def scratch_graph(docs_spec, *, identities=None):
    """A graph containing exactly this and nothing else, removed afterwards."""
    run_id = f"reconciletest{secrets.token_hex(4)}"
    with HydraClient() as client:
        client.verify()
        ids = []
        try:
            ids = build_graph(client, run_id, docs_spec, identities=identities)
            assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] == 0, (
                "the fixture must start with no conflict edges")
            yield client, run_id
        finally:
            if ids:
                client.bolt_write(
                    "UNWIND $rows AS row MATCH (n {id: row.id}) DETACH DELETE n",
                    {"rows": [{"id": i} for i in ids]})


@pytest.fixture
def disputed_graph():
    """Two documents disagreeing about one person's title, same raw predicate."""
    with scratch_graph({"dsid_rc_a": ("works as", "Staff Engineer"),
                        "dsid_rc_b": ("works as", "Director of Platform")}) as g:
        yield g


@pytest.mark.live
def test_a_dispute_between_two_documents_becomes_a_persisted_edge(disputed_graph):
    """The regression, end to end, starting from claims nothing had judged.

    The persisted edge is what the answer controller walks. Without it the
    disagreement never reaches an answer, however well detection works
    elsewhere.
    """
    client, run_id = disputed_graph
    outcome = reconcile_conflicts(client, run_id, "dsid_rc_a")

    assert outcome.facts_examined == 1
    assert outcome.conflicts_found >= 1, "the disagreement was not detected"
    assert outcome.edges_written >= 1, "the disagreement was not persisted"
    assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] >= 1, (
        "no CONFLICTS_WITH edge reached the graph")


@pytest.mark.live
def test_reconciling_repeatedly_does_not_duplicate_the_edge(disputed_graph):
    """Edge ids are deterministic, so re-adjudication converges.

    It runs on every ingest and the bulk sweep still exists, so the same pair is
    judged repeatedly. Two edges for one disagreement would double-count it
    everywhere it is shown.
    """
    client, run_id = disputed_graph
    reconcile_conflicts(client, run_id, "dsid_rc_a")
    first = client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"]
    assert first >= 1

    reconcile_conflicts(client, run_id, "dsid_rc_a")
    reconcile_conflicts(client, run_id, "dsid_rc_b")
    assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] == first, (
        "the same disagreement was written twice")


@pytest.mark.live
def test_the_controller_finds_what_reconciliation_wrote(disputed_graph):
    """The two halves have to meet, which is exactly where the bug lived.

    Detection wrote edges and the controller read edges, and nothing checked
    that the second could see the first.
    """
    from tracegraph.controller import AnswerController

    client, run_id = disputed_graph
    reconcile_conflicts(client, run_id, "dsid_rc_a")

    controller = AnswerController(client, run_id)
    used = [{"dsid": "dsid_rc_a", "subject": "Dana Okafor",
             "predicate": "works as", "object": "Staff Engineer", "quote": "q"}]
    found = controller.contested(
        used, asserted_in="Dana Okafor is a Staff Engineer.")

    assert found, "the controller could not see the edge reconciliation wrote"
    assert found[0]["rival_value"] == "Director of Platform"
    assert found[0]["rival_dsid"] == "dsid_rc_b"


# --- the two defects a same-predicate, no-identity fixture cannot see --------


@pytest.mark.live
def test_documents_disagreeing_under_different_predicate_spellings_still_conflict():
    """`works as` and `has job title` are one fact, so they must be compared.

    The incremental pass selected which facts to re-adjudicate by *raw*
    predicate while the adjudicator groups by the *canonical* one. Every pair
    phrased differently fell through the gap — 73 edges a full sweep found and
    the incremental pass did not, all of them aliases of `holds_title`.
    """
    with scratch_graph({"dsid_alias_a": ("works as", "Staff Engineer"),
                        "dsid_alias_b": ("has job title", "Director of Platform")}
                       ) as (client, run_id):
        # Only the second document is newly ingested, so selection has to reach
        # the first through the canonical name rather than the spelling.
        outcome = reconcile_conflicts(client, run_id, ["dsid_alias_b"])

        assert outcome.conflicts_found >= 1, (
            "a disagreement phrased two ways was not detected")
        assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] >= 1, (
            "no edge was written for a disagreement across predicate aliases")


@pytest.mark.live
def test_two_people_sharing_a_name_are_not_one_contested_fact():
    """Grouping on the name alone manufactured disputes between strangers.

    Anna Liu at cedarwave.com and Anna Liu at cloudwave.com were being reported
    as two versions of one person's employer, as were two Elena Rossis at
    unrelated companies. Thirty-one such edges were in the graph. Where the
    resolver has decided who a surface refers to, that decides whether two
    claims are about the same subject at all.
    """
    with scratch_graph(
        {"dsid_ident_a": ("works as", "Staff Engineer"),
         "dsid_ident_b": ("works as", "Director of Platform")},
        identities={"dsid_ident_a": "email:dana@northwind.com",
                    "dsid_ident_b": "email:dana@southgate.com"},
    ) as (client, run_id):
        outcome = reconcile_conflicts(client, run_id, ["dsid_ident_a", "dsid_ident_b"])

        assert outcome.conflicts_found == 0, (
            "two different people were adjudicated as one contested fact")
        assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] == 0


@pytest.mark.live
def test_one_person_under_two_documents_still_conflicts_when_identity_agrees():
    """The other half of the same rule — resolving must not suppress real disputes."""
    with scratch_graph(
        {"dsid_same_a": ("works as", "Staff Engineer"),
         "dsid_same_b": ("has job title", "Director of Platform")},
        identities={"dsid_same_a": "email:dana@northwind.com",
                    "dsid_same_b": "email:dana@northwind.com"},
    ) as (client, run_id):
        outcome = reconcile_conflicts(client, run_id, ["dsid_same_b"])

        assert outcome.conflicts_found >= 1, (
            "one person contradicted about their own title was not detected")
        assert client.bolt_read(CONFLICT_COUNT, {"r": run_id})[0]["n"] >= 1


def test_grouping_prefers_identity_over_name():
    """The unit behind both tests above, without a database."""
    same_name = [record("dsid_a", "Staff Engineer", claim_id=1),
                 record("dsid_b", "Director of Platform", claim_id=2)]

    by_name, _ = detect_conflicts(same_name, document_order=["dsid_a", "dsid_b"])
    assert len(by_name) == 1, "the baseline must be a conflict when only names are known"

    strangers, _ = detect_conflicts(
        same_name, document_order=["dsid_a", "dsid_b"],
        subject_identity={("dsid_a", SUBJECT.casefold()): 111,
                          ("dsid_b", SUBJECT.casefold()): 222})
    assert strangers == [], "two identities were merged into one contested fact"

    one_person, _ = detect_conflicts(
        same_name, document_order=["dsid_a", "dsid_b"],
        subject_identity={("dsid_a", SUBJECT.casefold()): 111,
                          ("dsid_b", SUBJECT.casefold()): 111})
    assert len(one_person) == 1, "one person's genuine dispute was suppressed"


def test_a_subject_with_no_resolved_identity_still_groups_by_name():
    """Most subjects are not people, and must not stop being adjudicated."""
    conflicts, _ = detect_conflicts(
        [record("dsid_a", "open", claim_id=1, subject="EX-011 remediation",
                predicate="has status"),
         record("dsid_b", "closed", claim_id=2, subject="EX-011 remediation",
                predicate="has status")],
        document_order=["dsid_a", "dsid_b"],
        # An identity map that knows about somebody else entirely must not stop
        # a subject that is not a person from being adjudicated.
        subject_identity={("dsid_a", "somebody else"): 111})
    assert len(conflicts) == 1


# --- the key both halves must agree on ---------------------------------------


def test_selection_and_adjudication_cannot_disagree():
    """One definition of "the same fact", because two was the bug — twice.

    Selection computed the key one way and adjudication another, so the
    incremental pass silently missed pairs the sweep found: first over predicate
    spelling (`has job title` never reached `works as`, 73 edges), then over
    subject spelling (`S. Ratnaparkhi` never reached `Sam`, though the resolver
    had already decided they are one person).
    """
    identity = {("d1", "s. ratnaparkhi"): 42, ("d2", "sam"): 42}
    assert (group_key("d1", "S. Ratnaparkhi", "works as", identity)
            == group_key("d2", "Sam", "has job title", identity)), (
        "two spellings of one resolved person are one fact")


def test_two_spellings_are_not_one_fact_without_a_resolved_identity():
    """Nothing is assumed. Absent a resolution, different names stay different."""
    assert (group_key("d1", "S. Ratnaparkhi", "works as", {})
            != group_key("d2", "Sam", "works as", {}))


@pytest.mark.parametrize("predicate", ["works as", "has job title",
                                       "current title", "holds title"])
def test_every_alias_of_one_predicate_reaches_the_same_fact(predicate):
    assert (group_key("d", SUBJECT, predicate, {})
            == group_key("d", SUBJECT, "works as", {}))


def test_an_unalignable_predicate_is_not_a_fact():
    """An unmapped relation is a queue item, not a licence to invent a category."""
    assert group_key("d", SUBJECT, "sent email on", {}) is None


@pytest.mark.live
def test_the_real_ingestion_writer_records_which_identity_it_chose():
    """Through the production writer, not a hand-populated fixture.

    Conflict adjudication reads `Mention.entity` to tell two people with one
    name apart. Both resolved-status builders in the ingestor omitted it while
    the tests set it by hand, so the property the whole mechanism depends on was
    never actually produced by the code that ships.
    """
    import inspect
    import re

    from tracegraph import ingest

    source = inspect.getsource(ingest.OnDemandIngestor._resolve_mentions)
    writes = re.findall(r"statuses\.append\(\{(.*?)\}\)", source, re.S)
    assert writes, "the status writer moved; this test needs updating"
    for block in writes:
        status = re.search(r'"status": "(\w+)"', block).group(1)
        assert '"entity"' in block, (
            f"a {status} mention is written without the identity it resolved "
            "to, which conflict adjudication reads")

    properties = re.search(r"properties=\[([^\]]*)\]\)\s*$", source.rstrip(), re.S)
    assert properties is None or "entity" in properties.group(1)
