"""Streaming access to the EnterpriseRAG-Bench Parquet files.

Two jobs live here because they share the same reader plumbing.

**Streaming.** The documents file is 1.4 GB compressed and 2.5 GB decoded, and
the shipped layout puts all 511,962 rows in a *single* row group. Anything that
materialises a whole row group -- ``pq.read_table``, ``ParquetFile.read_row_group``
-- is an out-of-memory kill on a host that also runs a 6 GB database container.
Every read path here is therefore batch-granular with column projection, single
threaded: a measured full pass over ``content`` peaks at 1.45 GB and takes 5 s,
and a metadata-only pass at 115 MB.

**The gold-answer firewall.** :func:`read_questions` is the only function in the
codebase permitted to open the questions parquet, because that file carries its
own answer key alongside the question text. It whitelists against
``config.QUESTION_COLUMNS_ALLOWED``, refuses ``config.QUESTION_COLUMNS_FORBIDDEN``
loudly, and never widens a selection it cannot satisfy: an unexpected schema
must yield less, never more. A leak here is silent and makes every downstream
number untrustworthy, so the denial is structural rather than conventional.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tracegraph import config

logger = logging.getLogger(__name__)

# Rows per decoded batch. 1024 rows of `content` is a few MB; the reader's own
# column-chunk buffers dominate the footprint, so shrinking this further buys
# nothing measurable.
DEFAULT_BATCH_ROWS = 1024

# Fetching one document decodes from the start of its row group, so a smaller
# batch means less over-read past the target row.
FETCH_BATCH_ROWS = 256

# Row group size for `repartition`. 2048 rows of `content` is roughly 10 MB
# decoded, which is what makes a single-document fetch affordable.
DEFAULT_ROW_GROUP_ROWS = 2048

DOC_ID_COLUMN = "doc_id"


class GoldAccessError(RuntimeError):
    """Raised when the questions parquet is asked for something it must not give.

    Covers both directions of the firewall: an explicit request for a withheld
    column, and a whitelist that leaves nothing readable (which must fail rather
    than degrade into "read everything").
    """


# --- Reading ----------------------------------------------------------------


def _open(path: Path | str) -> pq.ParquetFile:
    return pq.ParquetFile(Path(path))


def _resolve_columns(
    available: Sequence[str], columns: Iterable[str] | None, *, source: str
) -> list[str] | None:
    """Validate a projection against the file, preserving the caller's order."""
    if columns is None:
        return None
    requested = list(dict.fromkeys(columns))
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise ValueError(
            f"{source} has no column(s) {unknown}; available: {list(available)}"
        )
    return requested


def document_schema(path: Path | str = config.DOCUMENTS_PARQUET) -> list[str]:
    """Column names, read from the footer without decoding any data."""
    return list(_open(path).schema_arrow.names)


def iter_row_groups(
    path: Path | str,
    columns: Sequence[str] | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_ROWS,
) -> Iterator[tuple[int, pa.Table]]:
    """Stream ``(row_group_index, table)`` chunks, never a whole row group at once.

    A row group larger than ``batch_size`` is emitted as several tables that all
    carry the same index. That is not a detail: the shipped corpus is one row
    group of 511,962 rows, so honouring "one table per row group" literally
    would be the OOM this module exists to avoid.
    """
    parquet = _open(path)
    projection = _resolve_columns(
        parquet.schema_arrow.names, columns, source=str(path)
    )
    for row_group in range(parquet.metadata.num_row_groups):
        for batch in parquet.iter_batches(
            batch_size=batch_size,
            row_groups=[row_group],
            columns=projection,
            use_threads=False,
        ):
            yield row_group, pa.Table.from_batches([batch])


def iter_documents(
    path: Path | str = config.DOCUMENTS_PARQUET,
    columns: Sequence[str] | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_ROWS,
) -> Iterator[dict]:
    """Yield one plain dict per document, in file order.

    Schema is ``doc_id, source_type, title, content``. Pass ``columns`` to leave
    ``content`` on disk -- a metadata-only pass is roughly forty times cheaper.
    """
    for _row_group, table in iter_row_groups(path, columns, batch_size=batch_size):
        yield from table.to_pylist()


# --- Gold-answer firewall ---------------------------------------------------


def question_schema(path: Path | str = config.QUESTIONS_PARQUET) -> list[str]:
    """Every column name in the questions file, including the withheld ones.

    Names are metadata, not answers. Exposing them lets tests assert which
    forbidden columns this particular file actually carries, which is the only
    way to prove the firewall has something to do.
    """
    return list(_open(path).schema_arrow.names)


def read_questions(
    path: Path | str = config.QUESTIONS_PARQUET,
    columns: Sequence[str] | None = None,
) -> Iterator[dict]:
    """Read question text and metadata, and nothing that could leak the answer.

    The only sanctioned door to the questions parquet. Selection is the
    intersection of the caller's request, ``config.QUESTION_COLUMNS_ALLOWED``,
    and the file's real schema -- so a schema change removes columns rather than
    adding them. An explicit request for a forbidden column is a bug in the
    caller and raises immediately, whether or not the file has that column.
    """
    forbidden = frozenset(config.QUESTION_COLUMNS_FORBIDDEN)

    if columns is not None:
        requested = list(dict.fromkeys(columns))
        denied = [name for name in requested if name in forbidden]
        if denied:
            raise GoldAccessError(
                f"refusing to read gold-answer column(s) {denied} from {path}; "
                "only evaluation code may touch them, and never through this reader"
            )
    else:
        requested = None

    present = frozenset(question_schema(path))
    selected = [name for name in config.QUESTION_COLUMNS_ALLOWED if name in present]
    if requested is not None:
        wanted = frozenset(requested)
        selected = [name for name in selected if name in wanted]

    # Defence in depth: if the two config lists ever overlap, fail rather than
    # let an allowed-looking name carry an answer through.
    leaked = [name for name in selected if name in forbidden]
    if leaked:
        raise GoldAccessError(
            f"config lists {leaked} as both allowed and forbidden; refusing to read"
        )

    if not selected:
        raise GoldAccessError(
            f"no readable question columns in {path}: allowed "
            f"{list(config.QUESTION_COLUMNS_ALLOWED)}, present {sorted(present)}"
            + (f", requested {requested}" if requested is not None else "")
        )

    parquet = _open(path)
    for batch in parquet.iter_batches(
        batch_size=DEFAULT_BATCH_ROWS, columns=selected, use_threads=False
    ):
        yield from batch.to_pylist()


# --- Row location -----------------------------------------------------------

_LOCATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_location (
    doc_id    TEXT    NOT NULL PRIMARY KEY,
    row_group INTEGER NOT NULL,
    row_index INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS indexed_row_group (
    row_group INTEGER NOT NULL PRIMARY KEY,
    row_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_fingerprint (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class BuildReport:
    """What a (possibly resumed) index build actually did."""

    row_groups_indexed: int
    row_groups_skipped: int
    rows_indexed: int


class RowLocator:
    """A ``doc_id -> (row_group, row_index)`` map, kept in SQLite.

    The FTS index is contentless by design, so it can point at a document but
    cannot return its text. Snippet rendering and evidence-span validation both
    need the body of one specific document, and rescanning 1.4 GB to get it is
    not an option. This turns that into a primary-key lookup plus the decode of
    a single row group.

    That last cost is set by the file's layout, not by this class: on the corpus
    as shipped -- one row group holding everything -- a fetch decodes the whole
    file. Run :func:`repartition` first and index the copy.
    """

    def __init__(self, parquet_path: Path | str, db_path: Path | str) -> None:
        self.parquet_path = Path(parquet_path)
        self.db_path = Path(db_path)
        self.build_report: BuildReport | None = None

        self._parquet = _open(self.parquet_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(_LOCATOR_SCHEMA)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._check_fingerprint()

    # -- lifecycle --

    def close(self) -> None:
        self._conn.close()
        self._parquet.close()

    def __enter__(self) -> RowLocator:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- fingerprint --

    def _fingerprint(self) -> dict[str, str]:
        metadata = self._parquet.metadata
        return {
            "source_name": self.parquet_path.name,
            "source_bytes": str(self.parquet_path.stat().st_size),
            "num_rows": str(metadata.num_rows),
            "num_row_groups": str(metadata.num_row_groups),
        }

    def _check_fingerprint(self) -> None:
        """Resuming is only sound if the file is the one we started on."""
        current = self._fingerprint()
        stored = dict(
            self._conn.execute("SELECT key, value FROM source_fingerprint").fetchall()
        )
        if not stored:
            self._conn.executemany(
                "INSERT INTO source_fingerprint (key, value) VALUES (?, ?)",
                sorted(current.items()),
            )
            self._conn.commit()
            return
        if stored != current:
            raise ValueError(
                f"{self.db_path} was built from a different parquet file "
                f"({stored} != {current}); delete it and rebuild"
            )

    # -- build --

    @classmethod
    def build(
        cls,
        parquet_path: Path | str,
        db_path: Path | str,
        *,
        batch_log_every: int = 50_000,
    ) -> RowLocator:
        """Index every row group not already recorded, and return the locator.

        Resumable: each row group is written and marked in one transaction, so
        an interrupted build leaves whole groups done and the rest untouched.
        ``batch_log_every`` is a row count, not a batch count.
        """
        locator = cls(parquet_path, db_path)
        locator.build_report = locator._index(batch_log_every=batch_log_every)
        return locator

    def _index(self, *, batch_log_every: int) -> BuildReport:
        total_groups = self._parquet.metadata.num_row_groups
        done = self.indexed_row_groups()
        indexed = skipped = rows_indexed = 0

        for row_group in range(total_groups):
            if row_group in done:
                skipped += 1
                logger.debug(
                    "row group %d/%d already indexed, skipping", row_group, total_groups
                )
                continue

            count = self._index_row_group(row_group, batch_log_every=batch_log_every)
            indexed += 1
            rows_indexed += count
            logger.info(
                "row group %d/%d indexed: %d rows (%d rows this run)",
                row_group,
                total_groups,
                count,
                rows_indexed,
            )

        report = BuildReport(indexed, skipped, rows_indexed)
        logger.info(
            "locator build finished: %d row groups indexed, %d skipped, %d rows",
            report.row_groups_indexed,
            report.row_groups_skipped,
            report.rows_indexed,
        )
        return report

    def _index_row_group(self, row_group: int, *, batch_log_every: int) -> int:
        row_index = 0
        since_log = 0
        try:
            for batch in self._parquet.iter_batches(
                batch_size=DEFAULT_BATCH_ROWS,
                row_groups=[row_group],
                columns=[DOC_ID_COLUMN],
                use_threads=False,
            ):
                doc_ids = batch.column(0).to_pylist()
                # Rows stream straight into the open transaction so neither the
                # batch list nor a pending-insert buffer scales with the corpus.
                self._conn.executemany(
                    "INSERT INTO doc_location (doc_id, row_group, row_index) "
                    "VALUES (?, ?, ?)",
                    (
                        (doc_id, row_group, row_index + offset)
                        for offset, doc_id in enumerate(doc_ids)
                    ),
                )
                row_index += len(doc_ids)
                since_log += len(doc_ids)
                if since_log >= batch_log_every:
                    logger.info(
                        "row group %d: %d rows indexed so far", row_group, row_index
                    )
                    since_log = 0

            self._conn.execute(
                "INSERT INTO indexed_row_group (row_group, row_count) VALUES (?, ?)",
                (row_group, row_index),
            )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(
                f"duplicate {DOC_ID_COLUMN} while indexing row group {row_group} of "
                f"{self.parquet_path}; document ids must be unique ({exc})"
            ) from exc
        except BaseException:
            self._conn.rollback()
            raise

        self._conn.commit()
        return row_index

    # -- query --

    def indexed_row_groups(self) -> set[int]:
        rows = self._conn.execute("SELECT row_group FROM indexed_row_group").fetchall()
        return {row[0] for row in rows}

    def __len__(self) -> int:
        return self._conn.execute("SELECT count(*) FROM doc_location").fetchone()[0]

    def fetch(
        self, doc_id: str, columns: Sequence[str] | None = None
    ) -> dict | None:
        """Return one document as a plain dict, or ``None`` if it is not indexed.

        Decodes a single row group and stops at the target row. ``doc_id`` is
        always read back and checked against the request: an off-by-one in the
        index would otherwise attach the wrong body to a citation, which is the
        one failure this whole pipeline cannot tolerate.
        """
        located = self._conn.execute(
            "SELECT row_group, row_index FROM doc_location WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if located is None:
            return None
        row_group, row_index = located

        projection = _resolve_columns(
            self._parquet.schema_arrow.names, columns, source=str(self.parquet_path)
        )
        if projection is not None and DOC_ID_COLUMN not in projection:
            projection = [DOC_ID_COLUMN, *projection]

        offset = 0
        for batch in self._parquet.iter_batches(
            batch_size=FETCH_BATCH_ROWS,
            row_groups=[row_group],
            columns=projection,
            use_threads=False,
        ):
            if row_index < offset + batch.num_rows:
                record = batch.slice(row_index - offset, 1).to_pylist()[0]
                if record[DOC_ID_COLUMN] != doc_id:
                    raise RuntimeError(
                        f"{self.db_path} points {doc_id} at row {row_index} of row "
                        f"group {row_group}, which holds {record[DOC_ID_COLUMN]!r}; "
                        "the index and the parquet file disagree"
                    )
                return record
            offset += batch.num_rows

        raise RuntimeError(
            f"{self.db_path} points {doc_id} at row {row_index} of row group "
            f"{row_group}, which has only {offset} rows"
        )


def repartition(
    src: Path | str,
    dst: Path | str,
    *,
    row_group_size: int = DEFAULT_ROW_GROUP_ROWS,
    log_every: int = 100_000,
) -> Path:
    """Rewrite a parquet file with small row groups, streaming and losslessly.

    Random access to one document costs a row-group decode, so a file whose
    single row group holds the entire corpus makes :meth:`RowLocator.fetch`
    degenerate into a full scan. Parquet has no finer seek unit, so the fix is
    the layout, not the reader. Run this once against the shipped documents
    file and point ingestion and the locator at the copy; content, schema, and
    row order are unchanged.
    """
    src_path, dst_path = Path(src), Path(dst)
    parquet = _open(src_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    since_log = 0
    with pq.ParquetWriter(dst_path, parquet.schema_arrow) as writer:
        # One `write_batch` call is one row group, so the read batch size sets
        # the output layout directly.
        for batch in parquet.iter_batches(batch_size=row_group_size, use_threads=False):
            writer.write_batch(batch)
            written += batch.num_rows
            since_log += batch.num_rows
            if since_log >= log_every:
                logger.info("repartitioned %d rows into %s", written, dst_path)
                since_log = 0
    parquet.close()
    logger.info(
        "repartitioned %d rows from %s into %s at %d rows per row group",
        written,
        src_path,
        dst_path,
        row_group_size,
    )
    return dst_path
