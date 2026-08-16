"""Contentless FTS5 lexical index over the document corpus.

Parquet stays the authoritative body store, so this index holds no text: the
FTS5 table is declared ``content=''`` and only the inverted index reaches disk.
That is what keeps the lexical layer inside the disk budget for a 512k-document
corpus, and it is also the constraint every caller has to respect. A contentless
table cannot return stored columns, cannot be UPDATEd, and cannot be DELETEd
from, so a half-written index has no row-wise repair. The build is therefore
transactional per batch, records the batches it finished, and resumes from the
last committed one; anything worse than an interrupted batch is a full rebuild.

The rowid is the 63-bit Document node id from ``tracegraph.ids.node_id``. A
SQLite rowid is a signed 64-bit column, so a 63-bit id round-trips exactly and a
search hit joins straight to the id registry and to HydraDB without a mapping
table of its own.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from time import time

from tracegraph.config import FTS_DB

FTS_TABLE = "docs_fts"
BATCH_TABLE = "fts_build_batches"

# Titles in this corpus are short and highly selective (ticket keys, thread
# subjects, file names), so a title hit is worth several body hits. The weights
# live in the table's rank configuration so ordering and the returned score can
# never disagree.
TITLE_WEIGHT = 4.0
CONTENT_WEIGHT = 1.0

# Large enough that per-transaction overhead disappears, small enough that an
# interrupted build loses seconds of work rather than minutes.
DEFAULT_COMMIT_EVERY_ROWS = 5000

# Highest value a 63-bit masked node id can take; a wider value would still fit
# the rowid but means the id was never masked, which is a bug worth failing on.
MAX_NODE_ID = 2**63 - 1

IndexRow = tuple[int, str, str]

# A balanced double-quoted span in the user's question is an explicit phrase
# request. Curly quotes are included because questions are pasted from chat and
# mail clients that substitute them.
_QUOTED_SPAN = re.compile(r'"([^"]*)"|“([^”]*)”')

# Runs of letters and digits, matching what unicode61 indexes. Everything else
# -- quotes, asterisks, colons, hyphens, parentheses, punctuation -- is FTS5
# query syntax and is dropped rather than escaped.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class BuildStats:
    """Outcome of one `build_index` run, for the ingestion manifest."""

    rows_indexed: int
    batches_committed: int
    batches_skipped: int
    seconds: float


@dataclass(frozen=True)
class IndexStats:
    """Disk footprint and coverage of an existing index."""

    path: Path
    bytes_on_disk: int
    row_count: int
    batch_count: int

    @property
    def megabytes(self) -> float:
        return self.bytes_on_disk / (1024 * 1024)


def build_index(
    rows_iter: Iterable[IndexRow],
    db_path: Path | str = FTS_DB,
    commit_every_rows: int = DEFAULT_COMMIT_EVERY_ROWS,
) -> BuildStats:
    """Bulk-build the lexical index from ``(node_id, title, content)`` rows.

    Rows are cut into fixed-size batches and each batch is written with its
    completion record in a single transaction, so the index and the record of
    what is in it cannot disagree. Re-running skips the batches already
    recorded, which is the only available resume strategy: duplicate inserts
    into a contentless table cannot be deleted afterwards.

    Resumption is positional, so ``rows_iter`` must yield the same rows in the
    same order on every run. Appending documents to the end of the corpus is
    safe; reordering or inserting is not, and is detected rather than tolerated.
    """
    if commit_every_rows < 1:
        raise ValueError("commit_every_rows must be at least 1")

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    started = time()
    rows_indexed = 0
    committed = 0
    skipped = 0

    conn = _connect(db_path, writable=True)
    try:
        _ensure_schema(conn)
        recorded = _completed_batches(conn)

        for batch_no, batch in enumerate(_chunked(rows_iter, commit_every_rows)):
            previous = recorded.get(batch_no)
            if previous is not None:
                _check_replay(batch_no, batch, previous)
                skipped += 1
                continue
            _write_batch(conn, batch_no, batch)
            committed += 1
            rows_indexed += len(batch)
    finally:
        # Folding the WAL back makes the reported index size honest. A failure
        # here must never mask the error that ended the build.
        with suppress(sqlite3.Error):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

    return BuildStats(
        rows_indexed=rows_indexed,
        batches_committed=committed,
        batches_skipped=skipped,
        seconds=time() - started,
    )


def search(
    query: str,
    limit: int = 20,
    db_path: Path | str = FTS_DB,
) -> list[tuple[int, float]]:
    """Return ``(document_node_id, bm25_score)`` for a raw user question, best first.

    The question is sanitised here rather than by the caller, because an
    unsanitised question is an FTS5 syntax error rather than a poor result.
    Scores are negated from SQLite's convention so that larger is better, which
    is what score-level fusion with the embedding index expects.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")

    match = sanitise_query(query)
    if not match:
        return []

    conn = _connect(Path(db_path), writable=False)
    try:
        rows = conn.execute(
            f"SELECT rowid, rank FROM {FTS_TABLE} "
            f"WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    finally:
        conn.close()
    return [(int(rowid), -float(score)) for rowid, score in rows]


def sanitise_query(text: str) -> str:
    """Turn a natural-language question into a valid FTS5 MATCH expression.

    Questions arrive with characters FTS5 reads as syntax -- quotes, asterisks,
    colons, hyphens, parentheses -- and with bare AND/OR/NOT/NEAR keywords.
    Every surviving token is emitted as a quoted string, which makes keywords
    literal terms and punctuation a non-event.

    Terms are joined with OR: this index discovers candidates for the graph to
    reason over, and requiring every token of a full sentence would return
    nothing. bm25 still ranks documents matching more of the question first.
    Spans the user quoted are kept as phrases, which is the only phrase intent
    that can be recovered without guessing.
    """
    phrases: list[str] = []

    def _lift_phrase(match: re.Match[str]) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        tokens = _TOKEN.findall(inner or "")
        if tokens:
            phrases.append(" ".join(tokens))
        return " "

    remainder = _QUOTED_SPAN.sub(_lift_phrase, text)
    terms = phrases + _TOKEN.findall(remainder)

    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(term)

    return " OR ".join(f'"{term}"' for term in unique)


def index_stats(db_path: Path | str = FTS_DB) -> IndexStats:
    """Report index size and coverage for the disk budget.

    Size includes the WAL and shared-memory sidecars, since those are real bytes
    on the volume until a checkpoint folds them back.
    """
    db_path = Path(db_path)
    total = sum(
        path.stat().st_size
        for path in (db_path, _sidecar(db_path, "-wal"), _sidecar(db_path, "-shm"))
        if path.exists()
    )

    conn = _connect(db_path, writable=False)
    try:
        # count(*) works on a contentless table because columnsize defaults to 1
        # and the per-row size table is maintained; the text itself is absent.
        row_count = conn.execute(f"SELECT count(*) FROM {FTS_TABLE}").fetchone()[0]
        batch_count = conn.execute(f"SELECT count(*) FROM {BATCH_TABLE}").fetchone()[0]
    finally:
        conn.close()

    return IndexStats(
        path=db_path,
        bytes_on_disk=total,
        row_count=int(row_count),
        batch_count=int(batch_count),
    )


# --- internals --------------------------------------------------------------


def _connect(db_path: Path, *, writable: bool) -> sqlite3.Connection:
    """Open the index database with pragmas suited to the access pattern.

    The build runs in WAL with synchronous=NORMAL rather than with the journal
    disabled outright. Turning the journal off is faster but removes rollback,
    and an interrupted batch would then leave partial rows in a table that
    cannot be deleted from -- exactly the state resumability exists to avoid.
    """
    if not writable and not db_path.exists():
        # sqlite3.connect would create an empty database here, turning "the
        # index was never built" into a confusing "no such table" much later.
        raise FileNotFoundError(f"no FTS index at {db_path}; run the build first")

    conn = sqlite3.connect(db_path, isolation_level="")
    if writable:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-64000")
    else:
        # Read paths open read-write on purpose: a WAL database cannot be opened
        # in SQLite's read-only mode without its -shm sidecar. query_only gives
        # the same protection without needing the file to be writable.
        conn.execute("PRAGMA query_only=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
            f"USING fts5(title, content, content='')"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {BATCH_TABLE} ("
            "  batch_no INTEGER PRIMARY KEY,"
            "  row_count INTEGER NOT NULL,"
            "  first_rowid INTEGER NOT NULL,"
            "  last_rowid INTEGER NOT NULL,"
            "  committed_at REAL NOT NULL"
            ")"
        )
        conn.execute(
            f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rank) VALUES('rank', ?)",
            (f"bm25({TITLE_WEIGHT}, {CONTENT_WEIGHT})",),
        )


def _completed_batches(conn: sqlite3.Connection) -> dict[int, tuple[int, int, int]]:
    rows = conn.execute(
        f"SELECT batch_no, row_count, first_rowid, last_rowid FROM {BATCH_TABLE}"
    ).fetchall()
    return {int(no): (int(count), int(first), int(last)) for no, count, first, last in rows}


def _write_batch(
    conn: sqlite3.Connection, batch_no: int, batch: Sequence[IndexRow]
) -> None:
    """Write one batch and its completion record in a single transaction."""
    with conn:
        conn.executemany(
            f"INSERT INTO {FTS_TABLE}(rowid, title, content) VALUES (?, ?, ?)",
            (_checked(row) for row in batch),
        )
        conn.execute(
            f"INSERT INTO {BATCH_TABLE}"
            "(batch_no, row_count, first_rowid, last_rowid, committed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_no, len(batch), batch[0][0], batch[-1][0], time()),
        )


def _checked(row: IndexRow) -> IndexRow:
    node_id, title, content = row
    if not isinstance(node_id, int) or isinstance(node_id, bool):
        raise TypeError(f"node id must be an int, got {type(node_id).__name__}")
    if not 0 <= node_id <= MAX_NODE_ID:
        raise ValueError(f"node id {node_id} is not a 63-bit non-negative id")
    return node_id, title or "", content or ""


def _check_replay(
    batch_no: int, batch: Sequence[IndexRow], previous: tuple[int, int, int]
) -> None:
    """Fail loudly when a resumed run does not replay the batch it skips.

    Skipping a batch is only sound if it holds the same rows as the committed
    one. Silently skipping a different batch would leave documents permanently
    missing from an index that cannot be patched afterwards.
    """
    current = (len(batch), batch[0][0], batch[-1][0])
    if current != previous:
        raise ValueError(
            f"batch {batch_no} differs from the committed build "
            f"(rows/first/last {current} vs {previous}). The row source must "
            "replay in the same order; rebuild the index from scratch."
        )


def _chunked(rows: Iterable[IndexRow], size: int) -> Iterator[list[IndexRow]]:
    iterator = iter(rows)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


def _sidecar(db_path: Path, suffix: str) -> Path:
    return db_path.with_name(db_path.name + suffix)
