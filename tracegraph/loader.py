"""Batched, replay-safe ingestion over Bolt.

Two statement forms carry every write in this project, and both are verified
against the pinned engine in docs/engine-notes.md:

    UNWIND $rows AS row MERGE (n {id: row.vertex})
    SET n:Label, n.prop = row.prop

    UNWIND $rows AS row
    MATCH (s:Src {id: row.src}), (d:Dst {id: row.dst})
    MERGE (s)-[r:REL {id: row.eid}]->(d) SET r.prop = row.prop

Three engine behaviours shape everything here:

* Single-vertex `MERGE` outside `UNWIND` is rejected, so even a one-row upsert
  is a batch.
* The label is applied in `SET`, never folded into the `MERGE` pattern — the
  pattern is the identity being matched, and writing extra properties into it
  would rewrite what it matched.
* A `MERGE` that changes nothing still commits. Resume correctness therefore
  comes from id-keyed idempotency, never from counting created rows.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config
from .hydra_client import HydraClient, check_identifier

SCALAR_TYPES = (int, float, bool, str)


class RowValueError(ValueError):
    """A row carried a value the engine cannot store."""


@dataclass
class BatchStat:
    batch_key: str
    rows: int
    seconds: float

    @property
    def rows_per_second(self) -> float:
        return self.rows / self.seconds if self.seconds > 0 else 0.0


class Checkpointer:
    """Records which batches have committed, so a killed run resumes.

    A batch is marked complete only after the write returns. The reverse order
    would let a crash between write and mark look identical to a crash before
    the write, and the engine gives no way to tell them apart after the fact —
    a no-op `MERGE` commits exactly like a real one.
    """

    def __init__(self, db_path: Path | str = config.REGISTRY_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS loader_checkpoint (
                job        TEXT NOT NULL,
                batch_key  TEXT NOT NULL,
                rows       INTEGER NOT NULL,
                completed_at REAL NOT NULL,
                PRIMARY KEY (job, batch_key)
            )
            """
        )

    def done(self, job: str, batch_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM loader_checkpoint WHERE job = ? AND batch_key = ?",
            (job, batch_key),
        ).fetchone()
        return row is not None

    def mark(self, job: str, batch_key: str, rows: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO loader_checkpoint (job, batch_key, rows, completed_at) "
            "VALUES (?, ?, ?, ?)",
            (job, batch_key, rows, time.time()),
        )

    def completed_rows(self, job: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(rows), 0) FROM loader_checkpoint WHERE job = ?", (job,)
        ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()


def _validate_scalars(rows: Sequence[dict[str, Any]], *, context: str) -> None:
    """Reject non-scalar values before they reach the engine.

    Property values are integers, floats, booleans, and strings. The engine's
    own message for a violation names a row index and field but not the caller,
    and by then the batch has already crossed the wire; failing here says which
    row and which field while the caller still has the object.
    """
    for index, row in enumerate(rows):
        for key, value in row.items():
            if value is None:
                raise RowValueError(
                    f"{context}: row {index} field {key!r} is None. The engine "
                    "has no null; omit the property or use a sentinel."
                )
            if isinstance(value, bool) or isinstance(value, SCALAR_TYPES):
                continue
            raise RowValueError(
                f"{context}: row {index} field {key!r} is {type(value).__name__}. "
                "Only int, float, bool, and str can be stored; model lists and "
                "structures as nodes and edges."
            )


def _chunks(rows: Iterable[dict], size: int) -> Iterator[list[dict]]:
    if size > config.MAX_BATCH_ITEMS:
        raise ValueError(
            f"batch size {size} exceeds the engine's admission-control ceiling of "
            f"{config.MAX_BATCH_ITEMS} items per UNWIND batch"
        )
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def upsert_nodes(
    client: HydraClient,
    label: str,
    rows: Iterable[dict[str, Any]],
    *,
    job: str,
    properties: Sequence[str] | None = None,
    batch_size: int = config.NODE_BATCH_SIZE,
    checkpointer: Checkpointer | None = None,
    on_batch: Any = None,
) -> list[BatchStat]:
    """Upsert nodes of one label.

    Each row needs a `vertex` key holding the deterministic 63-bit id; every
    other key becomes a property. `properties` fixes the property set across
    batches, which matters because the statement is built from the keys — rows
    with differing keys would otherwise silently produce different statements.
    """
    check_identifier(label, kind="label")
    stats: list[BatchStat] = []

    for index, batch in enumerate(_chunks(rows, batch_size)):
        batch_key = f"{label}:{index}"
        if checkpointer and checkpointer.done(job, batch_key):
            continue

        props = list(properties) if properties else [
            k for k in batch[0].keys() if k != "vertex"
        ]
        for prop in props:
            check_identifier(prop, kind="property")

        payload = []
        for row in batch:
            if "vertex" not in row:
                raise RowValueError(f"{label} batch {index}: row is missing 'vertex'")
            item = {"vertex": int(row["vertex"])}
            for prop in props:
                if prop in row:
                    item[prop] = row[prop]
            payload.append(item)
        _validate_scalars(payload, context=f"{label} batch {index}")

        assignments = ", ".join(f"n.{p} = row.{p}" for p in props)
        cypher = (
            "UNWIND $rows AS row MERGE (n {id: row.vertex}) "
            f"SET n:{label}" + (f", {assignments}" if assignments else "")
        )

        started = time.perf_counter()
        client.bolt_write(cypher, {"rows": payload})
        elapsed = time.perf_counter() - started

        # Only after the write returns.
        if checkpointer:
            checkpointer.mark(job, batch_key, len(payload))

        stat = BatchStat(batch_key=batch_key, rows=len(payload), seconds=elapsed)
        stats.append(stat)
        if on_batch:
            on_batch(stat)

    return stats


def upsert_edges(
    client: HydraClient,
    rel_type: str,
    rows: Iterable[dict[str, Any]],
    *,
    job: str,
    source_label: str,
    target_label: str,
    properties: Sequence[str] | None = None,
    batch_size: int = config.EDGE_BATCH_SIZE,
    checkpointer: Checkpointer | None = None,
    on_batch: Any = None,
) -> list[BatchStat]:
    """Upsert relationships of one type.

    Each row needs `src`, `dst`, and `eid` — the deterministic edge id.
    Endpoints must already exist: the statement matches them by label and id,
    and rows whose endpoints are absent are silently skipped by the engine
    rather than failing, so load every node batch before its edges.

    `MERGE` on the edge id rather than `CREATE`: `CREATE` replays into a second
    parallel edge between the same pair, which turns a resumed ingest into
    duplicated evidence.
    """
    check_identifier(rel_type, kind="relationship type")
    check_identifier(source_label, kind="label")
    check_identifier(target_label, kind="label")
    stats: list[BatchStat] = []

    for index, batch in enumerate(_chunks(rows, batch_size)):
        batch_key = f"{rel_type}:{index}"
        if checkpointer and checkpointer.done(job, batch_key):
            continue

        reserved = {"src", "dst", "eid"}
        props = list(properties) if properties else [
            k for k in batch[0].keys() if k not in reserved
        ]
        for prop in props:
            check_identifier(prop, kind="property")

        payload = []
        for row in batch:
            missing = reserved - row.keys()
            if missing:
                raise RowValueError(
                    f"{rel_type} batch {index}: row is missing {sorted(missing)}"
                )
            item = {
                "src": int(row["src"]),
                "dst": int(row["dst"]),
                "eid": int(row["eid"]),
            }
            for prop in props:
                if prop in row:
                    item[prop] = row[prop]
            payload.append(item)
        _validate_scalars(payload, context=f"{rel_type} batch {index}")

        assignments = ", ".join(f"r.{p} = row.{p}" for p in props)
        cypher = (
            "UNWIND $rows AS row "
            f"MATCH (s:{source_label} {{id: row.src}}), (d:{target_label} {{id: row.dst}}) "
            f"MERGE (s)-[r:{rel_type} {{id: row.eid}}]->(d)"
            + (f" SET {assignments}" if assignments else "")
        )

        started = time.perf_counter()
        client.bolt_write(cypher, {"rows": payload})
        elapsed = time.perf_counter() - started

        if checkpointer:
            checkpointer.mark(job, batch_key, len(payload))

        stat = BatchStat(batch_key=batch_key, rows=len(payload), seconds=elapsed)
        stats.append(stat)
        if on_batch:
            on_batch(stat)

    return stats
