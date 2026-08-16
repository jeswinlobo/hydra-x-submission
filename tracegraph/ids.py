"""Deterministic identifiers and the collision registry.

HydraDB matches node identity by id alone, so an id is not a handle onto a
node: it *is* the node. Ids are therefore derived from content rather than
allocated, which makes ingestion replayable and lets a parser reference an
entity it has not written yet. The price of derivation is truncation: a
sha256 folded to 63 bits could in principle put two unrelated things at the
same address, and the engine would silently merge them. Every id minted here
is recorded in a SQLite registry that fails loudly the first time two
identities land on the same id, which is why the registry is a correctness
component rather than a debugging aid.

63 bits, not 64: HydraDB node ids are non-negative, and `2**63 - 1` is
verified to round-trip through Bolt exactly (docs/engine-notes.md).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from tracegraph.config import REGISTRY_DB

# ASCII unit separator. It cannot appear in any natural key we accept, so
# ("ab", "c") and ("a", "bc") cannot build the same hash payload.
UNIT_SEPARATOR = "\x1f"

ID_MASK = (1 << 63) - 1

KIND_NODE = "node"
KIND_EDGE = "edge"
KINDS = (KIND_NODE, KIND_EDGE)


# --- Key canonicalisation ---------------------------------------------------


class KeyCase(Enum):
    """Whether case is part of an identity or an accident of spelling."""

    PRESERVE = "preserve"
    FOLD = "fold"


# Keys are normalised (casefolded, underscores dropped) so that a label
# spelling, an entity-kind spelling, and a snake_case spelling of the same type
# — "ClaimGroup", "claim_group", "claimgroup" — resolve to one policy.
#
# The rule behind the table: names written by humans in prose fold, because
# "Sam Altman" and "sam altman" are one person and minting two nodes would
# split their evidence. Identifiers minted by some other system preserve case,
# because their case is part of the string and folding two of them together is
# an unrecoverable merge. Where a type is not listed the default preserves
# case: splitting one identity in two is something entity resolution can still
# repair later, merging two identities into one is not.
KEY_CASE: Mapping[str, KeyCase] = MappingProxyType(
    {
        # Human-written names and the lookup keys derived from them.
        "person": KeyCase.FOLD,
        "alias": KeyCase.FOLD,
        "email": KeyCase.FOLD,
        "handle": KeyCase.FOLD,
        "team": KeyCase.FOLD,
        "channel": KeyCase.FOLD,
        "company": KeyCase.FOLD,
        "product": KeyCase.FOLD,
        "project": KeyCase.FOLD,
        "topic": KeyCase.FOLD,
        "meeting": KeyCase.FOLD,
        "predicate": KeyCase.FOLD,
        "rawpredicate": KeyCase.FOLD,
        # Externally owned or system-minted identifiers.
        "document": KeyCase.PRESERVE,
        "dsid": KeyCase.PRESERVE,
        "ticket": KeyCase.PRESERVE,
        "pullrequest": KeyCase.PRESERVE,
        "repo": KeyCase.PRESERVE,
        "commit": KeyCase.PRESERVE,
        "url": KeyCase.PRESERVE,
        "class": KeyCase.PRESERVE,
        "claim": KeyCase.PRESERVE,
        "claimgroup": KeyCase.PRESERVE,
        "value": KeyCase.PRESERVE,
        "mention": KeyCase.PRESERVE,
        "evidencespan": KeyCase.PRESERVE,
    }
)

DEFAULT_KEY_CASE = KeyCase.PRESERVE


def key_case(node_type: str) -> KeyCase:
    """Return the canonicalisation policy for a node type.

    Only the policy lookup is lenient about spelling; the node type itself is
    hashed exactly as the caller wrote it.
    """
    return KEY_CASE.get(node_type.casefold().replace("_", ""), DEFAULT_KEY_CASE)


def canonical_key(node_type: str, raw: str) -> str:
    """Normalise a natural key so trivial spelling differences mint one id.

    Surrounding whitespace is stripped and internal runs collapse to a single
    space, because source templates pad and wrap freely. Case folds only for
    the types listed in KEY_CASE.
    """
    if UNIT_SEPARATOR in raw:
        raise ValueError(
            f"natural key for {node_type!r} contains the unit separator: {raw!r}"
        )
    key = " ".join(raw.split())
    if not key:
        raise ValueError(f"empty natural key for node type {node_type!r}")
    if key_case(node_type) is KeyCase.FOLD:
        key = key.casefold()
    return key


# --- Id derivation ----------------------------------------------------------


def _payload(type_name: str, key: str) -> str:
    if not type_name:
        raise ValueError("type name must be non-empty")
    if UNIT_SEPARATOR in type_name:
        raise ValueError(f"type name contains the unit separator: {type_name!r}")
    return f"{type_name}{UNIT_SEPARATOR}{key}"


def _node_payload(node_type: str, natural_key: str) -> str:
    if not natural_key:
        raise ValueError(f"empty natural key for node type {node_type!r}")
    if UNIT_SEPARATOR in natural_key:
        raise ValueError(
            f"natural key for {node_type!r} contains the unit separator: "
            f"{natural_key!r}"
        )
    return _payload(node_type, natural_key)


def _edge_key(source_id: int, target_id: int, scope: str) -> str:
    for endpoint in (source_id, target_id):
        if not 0 <= endpoint <= ID_MASK:
            raise ValueError(f"endpoint id out of 63-bit range: {endpoint}")
    if UNIT_SEPARATOR in scope:
        raise ValueError(f"edge scope contains the unit separator: {scope!r}")
    return f"{source_id}{UNIT_SEPARATOR}{target_id}{UNIT_SEPARATOR}{scope}"


def _digest(payload: str) -> bytes:
    # sha256, never the builtin hash(): hash() is randomised per process, so
    # ids minted by two workers would not agree.
    return hashlib.sha256(payload.encode("utf-8")).digest()


def _fold63(digest: bytes) -> int:
    return int.from_bytes(digest[:8], "big") & ID_MASK


def node_id(node_type: str, natural_key: str) -> int:
    """Derive a node's 63-bit id. The key must already be canonical."""
    return _fold63(_digest(_node_payload(node_type, natural_key)))


def node_full_hash(node_type: str, natural_key: str) -> str:
    """The untruncated digest, kept so a collision can be diagnosed later."""
    return _digest(_node_payload(node_type, natural_key)).hex()


def edge_id(
    edge_type: str, source_id: int, target_id: int, scope: str = ""
) -> int:
    """Derive a relationship's 63-bit id.

    Relationships carry identity in HydraDB, and MERGE keyed on this id is what
    makes replaying an edge batch idempotent instead of producing a parallel
    duplicate. `scope` separates edges that would otherwise be the same triple
    — an evidence span id, a validity scope, a thread — and is expected to
    arrive already normalised.
    """
    return _fold63(_digest(_payload(edge_type, _edge_key(source_id, target_id, scope))))


def edge_full_hash(
    edge_type: str, source_id: int, target_id: int, scope: str = ""
) -> str:
    """The untruncated digest for a relationship id."""
    return _digest(_payload(edge_type, _edge_key(source_id, target_id, scope))).hex()


class IdRow(NamedTuple):
    """One registered identity. Field order matches the `ids` table."""

    id: int
    kind: str
    node_type: str
    natural_key: str
    full_hash: str


def node_identity(node_type: str, raw_key: str) -> IdRow:
    """Canonicalise, hash, and package a node identity for the registry."""
    key = canonical_key(node_type, raw_key)
    digest = _digest(_node_payload(node_type, key))
    return IdRow(_fold63(digest), KIND_NODE, node_type, key, digest.hex())


def edge_identity(
    edge_type: str, source_id: int, target_id: int, scope: str = ""
) -> IdRow:
    """Hash and package a relationship identity for the registry.

    The stored natural key keeps the unit separators verbatim so that
    node_type plus natural_key reconstruct the exact hashed payload; a
    collision has to be reproducible from the registry alone.
    """
    key = _edge_key(source_id, target_id, scope)
    digest = _digest(_payload(edge_type, key))
    return IdRow(_fold63(digest), KIND_EDGE, edge_type, key, digest.hex())


# --- Registry ---------------------------------------------------------------


class IdCollisionError(RuntimeError):
    """Two distinct identities folded onto the same 63-bit id."""

    def __init__(self, stored: IdRow, offered: IdRow) -> None:
        self.stored = stored
        self.offered = offered
        super().__init__(
            f"id {stored.id} is registered to "
            f"{stored.kind}:{stored.node_type} key={stored.natural_key!r} "
            f"(sha256 {stored.full_hash}) but was offered for "
            f"{offered.kind}:{offered.node_type} key={offered.natural_key!r} "
            f"(sha256 {offered.full_hash})"
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ids (
    id          INTEGER PRIMARY KEY CHECK (id >= 0),
    kind        TEXT NOT NULL CHECK (kind IN ('node', 'edge')),
    node_type   TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    full_hash   TEXT NOT NULL
);
"""

_INSERT = (
    "INSERT OR IGNORE INTO ids (id, kind, node_type, natural_key, full_hash) "
    "VALUES (?, ?, ?, ?, ?)"
)

_COLUMNS = "id, kind, node_type, natural_key, full_hash"

_BUSY_TIMEOUT_MS = 10_000

# Well under SQLite's historical 999-variable limit.
_MAX_PARAMS = 900


class IdRegistry:
    """Every id ever minted, keyed by the id itself.

    The primary key is the id alone rather than (kind, id) or
    (node_type, id) because HydraDB resolves a node by id without consulting
    its label: a Document and an Entity that fold to the same 63-bit value are
    the same vertex to the engine, so that has to be a detected collision and
    not two happily coexisting rows. Nodes and edges share the table for the
    same reason it costs nothing — one table removes a whole class of mistake
    about which registry a caller should have used.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else REGISTRY_DB
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Ingestion writes the registry from worker threads while another
        # process may be reading it, hence WAL, the busy timeout, and the lock
        # guarding this single shared connection.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def register(
        self, id: int, kind: str, node_type: str, natural_key: str, full_hash: str
    ) -> bool:
        """Register one identity. True if it was new, False if already stored."""
        return self.register_many([IdRow(id, kind, node_type, natural_key, full_hash)]) == 1

    def register_many(self, rows: Iterable[IdRow | tuple]) -> int:
        """Register a batch and return how many rows were new.

        Re-offering an identity that is already stored is a no-op, which is
        what makes ingestion replayable. Offering a *different* identity for a
        stored id raises IdCollisionError and rolls the whole batch back, so a
        caller cannot half-commit a batch it was told to abandon.
        """
        offered = [IdRow(*row) for row in rows]
        if not offered:
            return 0
        for row in offered:
            if row.kind not in KINDS:
                raise ValueError(f"kind must be one of {KINDS}: {row!r}")
            if not 0 <= row.id <= ID_MASK:
                raise ValueError(f"id out of 63-bit range: {row!r}")
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany(_INSERT, offered)
            inserted = self._conn.total_changes - before
            self._verify(offered)
        return inserted

    def _verify(self, offered: list[IdRow]) -> None:
        """Read back what the insert left behind and prove it is what we offered.

        INSERT OR IGNORE swallows the primary-key conflict that a collision
        produces, so the conflict is only visible by comparison.
        """
        stored: dict[int, IdRow] = {}
        ids = [row.id for row in offered]
        for start in range(0, len(ids), _MAX_PARAMS):
            chunk = ids[start : start + _MAX_PARAMS]
            placeholders = ",".join("?" * len(chunk))
            cursor = self._conn.execute(
                f"SELECT {_COLUMNS} FROM ids WHERE id IN ({placeholders})", chunk
            )
            for row in cursor:
                stored[row[0]] = IdRow(*row)
        for row in offered:
            found = stored.get(row.id)
            if found is None:
                raise RuntimeError(f"registry lost a row it just inserted: {row!r}")
            if found != row:
                raise IdCollisionError(found, row)

    def lookup(self, id: int) -> IdRow | None:
        """Return the identity registered at an id, or None."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {_COLUMNS} FROM ids WHERE id = ?", (id,)
            )
            row = cursor.fetchone()
        return IdRow(*row) if row is not None else None

    def count(self, kind: str | None = None) -> int:
        """Number of registered identities, optionally restricted to one kind."""
        with self._lock:
            if kind is None:
                cursor = self._conn.execute("SELECT count(*) FROM ids")
            else:
                cursor = self._conn.execute(
                    "SELECT count(*) FROM ids WHERE kind = ?", (kind,)
                )
            return int(cursor.fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> IdRegistry:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
