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
