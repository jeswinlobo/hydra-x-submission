"""The values production actually runs with.

Every one of these was, at some point, changed for an experiment and reported as
reverted while the reverted value never landed — a string replacement that
silently matched nothing. A comment saying "reverted" is not a revert, so the
defaults are asserted here instead.
"""

from __future__ import annotations

import inspect

from tracegraph.controller import AnswerController
from tracegraph.ingest import OnDemandIngestor


def default(cls, name: str):
    return inspect.signature(cls.__init__).parameters[name].default


def test_synthesis_window_is_forty():
    """Raising this to 96 was measured and reverted.

    Ten rounds produced a crash, a wrong verdict, and synthesis calls of 105s,
    58s and 57s against a p50 of 8s. More evidence made answers slower and less
    stable, so the bound stays where measurement put it.
    """
    assert default(AnswerController, "evidence_window") == 40


def test_retrieval_keeps_eight_documents():
    """The number the README's headline recall figure is reported at."""
    assert default(AnswerController, "max_documents") == 8


def test_cold_enrichment_budget_is_four():
    assert default(AnswerController, "ingest_budget") == 4


def test_extraction_reads_sixteen_thousand_characters():
    """8,000 truncated 35.7% of the corpus and discarded 11.1% of all text."""
    assert default(OnDemandIngestor, "max_body") == 16000


def test_ingestion_resolves_and_reconciles_by_default():
    """Both are load-bearing: without them mentions stay undecided and
    conflicts stop being maintained for anything ingested on demand."""
    assert default(OnDemandIngestor, "resolve") is True
    assert default(OnDemandIngestor, "reconcile") is True
