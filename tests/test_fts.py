"""Tests for the contentless FTS5 lexical index.

Pure SQLite: no HydraDB, no corpus Parquet. The point of this layer is that it
can be verified on a laptop in milliseconds, so the build is exercised against a
handful of synthetic documents that carry the same shapes as the real ones.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from tracegraph.fts import (
    BATCH_TABLE,
    FTS_TABLE,
    MAX_NODE_ID,
    build_index,
    index_stats,
    sanitise_query,
    search,
)

# Every body mentions "acme" so a single term can be used to enumerate the whole
# index, which is how coverage and duplicate rows are checked.
DOCS: list[tuple[int, str, str]] = [
    (
        1001,
        "Atlas migration rollback plan",
        "The rollback plan for the Atlas migration is owned by the platform team "
        "at Acme and was signed off before the cutover window.",
    ),
    (
        1002,
        "Q3 revenue forecast",
        "Acme revenue forecast for the third quarter, prepared by finance.",
    ),
    (
        1003,
        "Onboarding checklist",
        "Checklist for new Acme engineers: laptop, accounts, and buddy assignment.",
    ),
    (
        1004,
        "Weekly platform sync notes",
        "Acme platform team discussed the deploy freeze and the on-call rotation.",
    ),
    (
        1005,
        "Vendor contract renewal",
        "Acme legal reviewed the renewal terms and flagged the liability cap.",
    ),
    (
        1006,
        "Incident 4417 postmortem",
        "Acme search latency regressed after a bad cache config was rolled out.",
    ),
    (
        1007,
        "Design review: notification service",
        "Acme notification service design review covering fan-out and retries.",
    ),
    (
        1008,
        "Hiring loop feedback",
        "Acme interview loop feedback for the backend candidate pool.",
    ),
]


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "fts.sqlite3"


def _built(db, docs=DOCS, commit_every_rows=1000):
    return build_index(docs, db, commit_every_rows=commit_every_rows)


def _raw_rowids(db) -> list[int]:
    """Read rowids straight out of SQLite, bypassing the module under test."""
    conn = sqlite3.connect(db)
    try:
        return [
            row[0]
            for row in conn.execute(
                f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH 'acme'"
            )
        ]
    finally:
        conn.close()


def test_ranking_puts_the_obviously_relevant_document_first(db):
    _built(db)

    hits = search("Atlas migration rollback plan", limit=5, db_path=db)

    assert hits, "a lexical query matching a title must return candidates"
    assert hits[0][0] == 1001
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True), "results must be best-first"


def test_rowid_round_trips_a_full_63_bit_node_id(db):
    full = MAX_NODE_ID
    assert full == 2**63 - 1
    _built(db, docs=[(full, "Atlas migration rollback plan", "acme platform")])

    hits = search("atlas migration", limit=5, db_path=db)

    assert [node_id for node_id, _ in hits] == [full]
    assert _raw_rowids(db) == [full]


def test_search_tolerates_fts5_syntax_in_a_natural_question(db):
    _built(db)

    question = 'Who owns the "atlas-migration" rollback plan (Q3) OR the notes*: draft?'
    hits = search(question, limit=5, db_path=db)

    assert hits, "a sanitised question must still retrieve candidates"
    assert hits[0][0] == 1001


def test_sanitise_query_quotes_terms_and_drops_punctuation():
    assert sanitise_query("rollback plan") == '"rollback" OR "plan"'
    # Bare boolean keywords become literal terms rather than operators; the
    # unicode61 tokenizer folds case on both sides, so casing is left alone.
    assert sanitise_query("deploy OR NOT freeze") == '"deploy" OR "OR" OR "NOT" OR "freeze"'
    # A hyphenated or colon-suffixed token contributes its parts, never syntax.
    assert sanitise_query("atlas-migration status:") == '"atlas" OR "migration" OR "status"'
    # Nothing survives stripping, so there is no query to run.
    assert sanitise_query("*** -- ?!") == ""
    assert sanitise_query("") == ""


def test_sanitise_query_preserves_an_explicitly_quoted_phrase():
    assert sanitise_query('the "deploy freeze" policy') == (
        '"deploy freeze" OR "the" OR "policy"'
    )
    # An unbalanced quote is a typo, not a phrase, and must not leak syntax.
    assert sanitise_query('the "deploy freeze') == '"the" OR "deploy" OR "freeze"'


def test_search_returns_nothing_when_the_question_has_no_terms(db):
    _built(db)
    assert search("*** ?!", limit=5, db_path=db) == []


def test_build_resumes_after_an_interrupted_batch(db):
    def interrupted(stop_after: int) -> Iterator[tuple[int, str, str]]:
        for position, row in enumerate(DOCS):
            if position == stop_after:
                raise RuntimeError("simulated crash mid-batch")
            yield row

    # Two batches of three commit; the fourth row of the third batch never
    # arrives, so that batch must roll back whole.
    with pytest.raises(RuntimeError):
        build_index(interrupted(7), db, commit_every_rows=3)

    partial = index_stats(db)
    assert partial.row_count == 6
    assert partial.batch_count == 2

    resumed = build_index(DOCS, db, commit_every_rows=3)

    assert resumed.batches_skipped == 2
    assert resumed.batches_committed == 1
    assert resumed.rows_indexed == 2

    final = index_stats(db)
    assert final.row_count == len(DOCS)
    assert final.batch_count == 3

    rowids = _raw_rowids(db)
    assert sorted(rowids) == sorted(node_id for node_id, _, _ in DOCS)
    assert len(rowids) == len(set(rowids)), "resuming must not duplicate rows"

    conn = sqlite3.connect(db)
    try:
        recorded = conn.execute(f"SELECT sum(row_count) FROM {BATCH_TABLE}").fetchone()[0]
    finally:
        conn.close()
    assert recorded == len(DOCS)


def test_rerunning_a_finished_build_writes_nothing(db):
    _built(db, commit_every_rows=3)
    again = build_index(DOCS, db, commit_every_rows=3)

    assert again.rows_indexed == 0
    assert again.batches_committed == 0
    assert again.batches_skipped == 3
    assert index_stats(db).row_count == len(DOCS)


def test_resuming_a_reordered_source_fails_loudly(db):
    build_index(DOCS[:3], db, commit_every_rows=3)

    with pytest.raises(ValueError, match="replay in the same order"):
        build_index(list(reversed(DOCS)), db, commit_every_rows=3)


def test_index_stats_reports_size_for_the_disk_budget(db):
    _built(db, commit_every_rows=3)
    stats = index_stats(db)

    assert stats.path == db
    assert stats.bytes_on_disk > 0
    assert stats.megabytes == stats.bytes_on_disk / (1024 * 1024)
    assert stats.row_count == len(DOCS)
    assert stats.batch_count == 3


def test_out_of_range_node_id_is_rejected(db):
    with pytest.raises(ValueError):
        build_index([(2**63, "too wide", "acme")], db)
    with pytest.raises(ValueError):
        build_index([(-1, "negative", "acme")], db)
