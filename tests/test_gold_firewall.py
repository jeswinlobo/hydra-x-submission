"""The answer key must not reach the thing being measured.

This is the difference between a real result and a cheating one, and until now
it was the one important claim in the project with no test behind it —
`read_questions` had a whitelist and zero callers exercising it.

The tests run against a synthetic parquet carrying the same column names as the
benchmark, so they prove the mechanism without reading a single real gold value.
One test does look at the real file, but only at its schema: column names are
metadata, and knowing which forbidden columns are present is what proves the
firewall has something to guard.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tracegraph import config
from tracegraph.parquet_reader import (
    GoldAccessError,
    question_schema,
    read_answer_key,
    read_questions,
)

FORBIDDEN = ("gold_answer", "answer_facts", "expected_doc_ids")


@pytest.fixture(scope="module")
def questions(tmp_path_factory) -> str:
    """A stand-in for the benchmark questions file, gold columns included."""
    path = tmp_path_factory.mktemp("firewall") / "questions.parquet"
    pq.write_table(pa.table({
        "question_id": ["q1", "q2"],
        "question": ["who owns the runbook?", "what is the retention policy?"],
        "question_type": ["basic", "constrained"],
        "source_types": [["slack"], ["gmail"]],
        "gold_answer": ["SECRET-A", "SECRET-B"],
        "answer_facts": [["SECRET-C"], ["SECRET-D"]],
        "expected_doc_ids": [["dsid_1"], ["dsid_2", "dsid_3"]],
    }), path)
    return str(path)


# --- the answering door ------------------------------------------------------

def test_default_read_returns_no_gold(questions):
    rows = list(read_questions(questions))
    assert rows, "the reader returned nothing at all"
    for row in rows:
        assert set(row) <= set(config.QUESTION_COLUMNS_ALLOWED)
        for column in FORBIDDEN:
            assert column not in row


@pytest.mark.parametrize("column", FORBIDDEN)
def test_asking_for_a_gold_column_raises(questions, column):
    with pytest.raises(GoldAccessError):
        list(read_questions(questions, columns=[column]))


def test_smuggling_gold_beside_an_allowed_column_raises(questions):
    """The request is rejected whole rather than quietly filtered.

    Filtering would let a caller believe it received what it asked for.
    """
    with pytest.raises(GoldAccessError):
        list(read_questions(questions, columns=["question", "gold_answer"]))


def test_a_whitelist_that_selects_nothing_raises(questions):
    """Failing closed. An empty selection must not degrade to "read everything"."""
    with pytest.raises(GoldAccessError):
        list(read_questions(questions, columns=["not_a_column"]))


# --- the scoring door --------------------------------------------------------

def test_answer_key_returns_the_key_and_nothing_else(questions):
    rows = list(read_answer_key(questions))
    assert [r["question_id"] for r in rows] == ["q1", "q2"]
    for row in rows:
        assert set(row) == {"question_id", "expected_doc_ids"}
        # Crucially, not the question text: a caller holding the key must not
        # also hold the input, or it is one refactor from feeding one to the other.
        assert "question" not in row
        assert "gold_answer" not in row and "answer_facts" not in row


def test_the_two_doors_cannot_be_collapsed_into_one(questions):
    """Neither reader can do the other's job, which is the whole point."""
    answering = set(list(read_questions(questions))[0])
    scoring = set(list(read_answer_key(questions))[0])
    assert "expected_doc_ids" not in answering
    assert "question" not in scoring
    assert answering & scoring == {"question_id"}, "only the join key crosses"


# --- the real file -----------------------------------------------------------

@pytest.mark.skipif(not config.QUESTIONS_PARQUET.exists(),
                    reason="needs the benchmark questions file")
def test_the_real_file_actually_carries_gold():
    """Schema only — names, never values.

    Without this the tests above could be guarding against columns the real
    corpus does not even have.
    """
    present = set(question_schema(config.QUESTIONS_PARQUET))
    assert set(FORBIDDEN) <= present, "the firewall guards nothing"
    rows = list(read_questions(config.QUESTIONS_PARQUET, columns=["question_id"]))
    assert rows and set(rows[0]) == {"question_id"}
