"""Shared configuration and constants.

Every module reads paths, connection settings, and the gold-answer firewall
list from here so there is exactly one place to change them.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Corpus -----------------------------------------------------------------

DATASET_DIR = REPO_ROOT / "dataset" / "EnterpriseRAG-Bench" / "data"
DOCUMENTS_PARQUET = DATASET_DIR / "documents" / "test.parquet"
QUESTIONS_PARQUET = DATASET_DIR / "questions" / "test.parquet"

# The nine enterprise sources the corpus is drawn from.
SOURCE_TYPES = (
    "slack",
    "gmail",
    "linear",
    "google_drive",
    "hubspot",
    "fireflies",
    "github",
    "jira",
    "confluence",
)

# --- Gold-answer firewall ---------------------------------------------------
#
# The questions parquet carries its own answer key. Ingestion and answering read
# the question text and nothing else; only evaluation may touch the rest. The
# reader whitelists rather than blacklists, so a schema change fails closed, but
# both lists are kept explicit because the failure is silent and the
# consequence — a submission whose results cannot be trusted — is terminal.
QUESTION_COLUMNS_ALLOWED = ("question_id", "question", "question_type", "source_types")
QUESTION_COLUMNS_FORBIDDEN = ("gold_answer", "answer_facts", "expected_doc_ids")

# `eval-oracle/employee_directory.yaml` maps every person to their email, title,
# and manager. Resolving aliases against it would answer the exact question the
# track poses, so it is scoring material only and is never read from
# `tracegraph/` outside an evaluation module.
EVAL_ORACLE_DIR = REPO_ROOT / "eval-oracle"

# --- Local indexes ----------------------------------------------------------
#
# Two SQLite files rather than one: the FTS bulk build holds long write
# transactions, and the id registry is read and written concurrently by
# ingestion. Separate files means neither blocks the other.

INDEX_DIR = REPO_ROOT / "indexes"
REGISTRY_DB = INDEX_DIR / "registry.sqlite3"
FTS_DB = INDEX_DIR / "fts.sqlite3"

# The corpus ships as a single row group holding all 511,962 documents, and a
# point lookup has to decode a row group whole. Fetching one document therefore
# costs four and a half seconds against the file as shipped — paid four times
# per question, since on-demand ingestion enriches several candidates. Answering
# spent longer scanning parquet than it did talking to the model.
#
# `scripts/71_repartition_corpus.py` writes a losslessly re-chunked copy here and
# indexes it. The original is never modified, and it stays the source of truth
# for bulk passes, which stream whole row groups and do not care.
LOCATOR_PARQUET = INDEX_DIR / "documents-rowgroups.parquet"

# The locator index lives apart from the id registry so the two can be rebuilt
# independently. Re-chunking invalidates the row map and nothing else; the
# registry holds 511,958 minted ids that must survive it.
LOCATOR_DB = INDEX_DIR / "locator.sqlite3"


class LocatorNotBuilt(RuntimeError):
    """The re-chunked corpus and its row map have not been built."""


def locator_parquet() -> Path:
    """The file point lookups read: the re-chunked copy.

    This used to fall back to the original corpus, described as "slowly, but
    working". It was neither. Nothing has populated a row map inside the id
    registry since the repartition became a pipeline step, and `RowLocator`
    creates its tables with `CREATE TABLE IF NOT EXISTS` while
    `_check_fingerprint` *stores* a fingerprint when none is present — so the
    fallback opened an empty index without raising, every fetch returned None,
    `_read` turned that into "not in corpus", and the system abstained on every
    question behind an HTTP 200. A fallback that silently answers nothing is
    worse than no fallback, so this now refuses.
    """
    _require_locator()
    return LOCATOR_PARQUET


def locator_db() -> Path:
    """The row map matching `locator_parquet`."""
    _require_locator()
    return LOCATOR_DB


def _require_locator() -> None:
    missing = [p for p in (LOCATOR_PARQUET, LOCATOR_DB) if not p.exists()]
    if missing:
        raise LocatorNotBuilt(
            f"no document row map at {', '.join(str(p) for p in missing)}; "
            "run scripts/71_repartition_corpus.py (bootstrap.sh does this). "
            "Without it no document body can be fetched, and every question "
            "would abstain."
        )

# --- HydraDB ----------------------------------------------------------------

HYDRA_BOLT_URI = os.getenv("HYDRA_BOLT_URI", "bolt://127.0.0.1:7687")
HYDRA_HTTP_URL = os.getenv("HYDRA_HTTP_URL", "http://127.0.0.1:8443")
HYDRA_GRAPH = os.getenv("HYDRA_GRAPH", "default")
HYDRA_NAMESPACE = os.getenv("HYDRA_NAMESPACE", "default")
HYDRA_CELL_ID = os.getenv("HYDRA_CELL_ID", "cell-0")
HYDRA_DATABASE = os.getenv("HYDRA_DATABASE", "default")
# Bolt auth is ("neo4j", <token>); the username is fixed and carries no meaning.
HYDRA_BOLT_USER = "neo4j"


def hydra_token() -> str:
    """Read the node's auth token, preferring the env var over the local file."""
    token = os.getenv("HYDRA_AUTH_TOKEN")
    if token:
        return token.strip()
    token_file = REPO_ROOT / "hydradb-data" / "auth-token"
    if token_file.exists():
        return token_file.read_text().strip()
    raise RuntimeError(
        "No HydraDB auth token. Set HYDRA_AUTH_TOKEN or run scripts/01_hydra_up.sh."
    )


# Batch sizes for Bolt ingestion.
#
# The engine enforces an admission-control ceiling of 1024 items per UNWIND
# batch: exceeding it fails the write with
# `client_query_batch_items rejected by admission control`. This is a hard
# limit, not a tuning knob, so these sit just below it.
MAX_BATCH_ITEMS = 1024
NODE_BATCH_SIZE = 1000
EDGE_BATCH_SIZE = 1000

# --- Models -----------------------------------------------------------------
#
# Pinned by full id so a moving alias cannot change extraction behaviour
# mid-corpus. The id actually returned by the API is recorded in every
# extraction manifest.
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
SYNTHESIS_MODEL = "claude-sonnet-5"

PROMPT_VERSION = "1"
SCHEMA_VERSION = "1"
