"""Shared fixtures for the live HydraDB contract suite.

The contract tests are only meaningful against a real node, so everything here
is built to make a missing node a clean skip rather than a red suite, and to
make a *leftover* node from an earlier run incapable of influencing a result.
Every node the suite writes is stamped with a per-session label, and the ids are
derived from that same session so two concurrent runs cannot collide.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx
import pytest
from neo4j import Driver, GraphDatabase, Record
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable

from tracegraph import config

# Cleanup deletes by id through UNWIND, which the engine admits at most 1024
# rows at a time (pinned by test_unwind_batch_admission_limit_is_1024).
_CLEANUP_BATCH = 1000

# A pathological cleanup loop is worse than leftover test nodes, so the drain
# is bounded. 200 * 1000 rows is far more than any test in this suite writes.
_CLEANUP_MAX_PASSES = 200

_LABEL_SUFFIX = re.compile(r"\A[A-Za-z0-9]*\Z")


# --- Deterministic ids ------------------------------------------------------
#
# PLAN.md fixes the identity rule as SHA-256 over the type and the canonical
# natural key, masked to 63 bits. The suite prefers tracegraph.ids so the tests
# exercise the same code path ingestion uses; the local fallback exists only so
# the engine contract can be pinned before that module lands, and implements the
# identical rule.

_MASK_63 = (1 << 63) - 1


def _fallback_node_id(node_type: str, natural_key: str) -> int:
    digest = hashlib.sha256(f"{node_type}\x00{natural_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & _MASK_63


def _fallback_edge_id(edge_type: str, source_id: int, target_id: int, scope: str) -> int:
    payload = f"{edge_type}\x00{source_id}\x00{target_id}\x00{scope}"
    digest = hashlib.sha256(payload.encode()).digest()
    return int.from_bytes(digest[:8], "big") & _MASK_63


try:  # pragma: no cover - which branch runs depends on repo state, not on input
    from tracegraph.ids import edge_id as _edge_id, node_id as _node_id
except ImportError:
    _node_id = _fallback_node_id
    _edge_id = _fallback_edge_id


def pytest_configure(config: pytest.Config) -> None:
    # pytest matches hook arguments by name against the hookspec; renaming this
    # parameter makes the plugin fail to register.
    config.addinivalue_line(
        "markers",
        "benchmark: measurement rather than assertion; deselect with -m 'not benchmark'",
    )


# --- Per-session isolation --------------------------------------------------


@dataclass
class RunScope:
    """Names and ids unique to one test session.

    Concurrent sessions and abandoned data from crashed sessions both exist in a
    shared graph. Scoping every label and every id to this session is what stops
    either of them from making an assertion pass or fail for the wrong reason.
    """

    run_id: str
    _labels: set[str] = field(default_factory=set)

    def label(self, suffix: str = "") -> str:
        """Return a session-unique Cypher label, remembered for teardown.

        Labels cannot be parameterised in Cypher, so callers interpolate the
        result into the query text. The suffix is therefore restricted to
        characters that cannot escape an identifier.
        """
        if not _LABEL_SUFFIX.match(suffix):
            raise ValueError(f"label suffix must be alphanumeric, got {suffix!r}")
        name = f"{self.run_id}{suffix}"
        self._labels.add(name)
        return name

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(sorted(self._labels))

    def node_id(self, node_type: str, natural_key: str) -> int:
        return _node_id(node_type, f"{self.run_id}:{natural_key}")

    def edge_id(self, edge_type: str, source_id: int, target_id: int, scope: str = "") -> int:
        return _edge_id(edge_type, source_id, target_id, f"{self.run_id}:{scope}")


@pytest.fixture(scope="session")
def run_scope() -> RunScope:
    """One label namespace per session, keyed by pid plus entropy.

    The pid alone is not enough: pids are recycled, and a session that crashed
    before teardown can leave nodes behind under the same name.
    """
    return RunScope(run_id=f"CT{os.getpid()}X{secrets.token_hex(4).upper()}")


# --- Transports -------------------------------------------------------------

_UNREACHABLE = (
    "HydraDB is not reachable at {target}. Start it with scripts/01_hydra_up.sh "
    "and re-run. ({error})"
)


class Bolt:
    """Thin session-per-query wrapper over the driver.

    HydraDB accepts exactly one statement per request, so there is no useful
    multi-statement transaction to hold open; a session per query is the honest
    shape and keeps a failing test from poisoning the next one.
    """

    def __init__(self, driver: Driver, database: str) -> None:
        self.driver = driver
        self.database = database

    def run(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a query and return rows as plain dicts."""
        return [record.data() for record in self.records(cypher, params)]

    def records(self, cypher: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Run a query and return driver Records, which preserve graph types."""
        with self.driver.session(database=self.database) as session:
            return list(session.run(cypher, params or {}))

    def bookmarks(self, cypher: str, params: dict[str, Any] | None = None) -> frozenset[str]:
        """Run a query and return the bookmarks the session ended up holding."""
        with self.driver.session(database=self.database) as session:
            session.run(cypher, params or {}).consume()
            return session.last_bookmarks().raw_values


@pytest.fixture(scope="session")
def bolt(run_scope: RunScope) -> Iterator[Bolt]:
    """Live Bolt driver, with best-effort teardown of this session's nodes."""
    try:
        token = config.hydra_token()
    except RuntimeError as exc:
        pytest.skip(_UNREACHABLE.format(target=config.HYDRA_BOLT_URI, error=exc))

    driver = GraphDatabase.driver(config.HYDRA_BOLT_URI, auth=(config.HYDRA_BOLT_USER, token))
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError) as exc:
        driver.close()
        pytest.skip(_UNREACHABLE.format(target=config.HYDRA_BOLT_URI, error=exc))

    client = Bolt(driver, config.HYDRA_DATABASE)
    try:
        yield client
    finally:
        _drop_run_nodes(client, run_scope)
        driver.close()


def _drop_run_nodes(bolt: Bolt, run_scope: RunScope) -> None:
    """Delete every node this session created, tolerating any failure.

    Deleting by label scan is what the engine kills on a query timeout once the
    label holds a few thousand nodes, so the ids are read first and deleted
    through the batched by-id form instead.
    """
    for label in run_scope.labels:
        for _ in range(_CLEANUP_MAX_PASSES):
            try:
                found = bolt.run(f"MATCH (n:{label}) RETURN n.id AS id LIMIT {_CLEANUP_BATCH}")
                if not found:
                    break
                bolt.run(
                    "UNWIND $rows AS row MATCH (n {id: row.vertex}) DETACH DELETE n",
                    {"rows": [{"vertex": row["id"]} for row in found]},
                )
            except (Neo4jError, OSError):
                break


class Http:
    """Caller for the HTTP query API, which is the transport that returns
    read_epoch and bookmark in the response body."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._path = f"/v1/graphs/{config.HYDRA_GRAPH}/query"

    def healthz(self) -> httpx.Response:
        return self._client.get("/healthz")

    def query(self, cypher: str) -> dict[str, Any]:
        """Execute a read and return the whole response body, epoch included."""
        response = self._client.post(
            self._path, json={"cell_id": config.HYDRA_CELL_ID, "query": cypher}
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture(scope="session")
def http() -> Iterator[Http]:
    """Live HTTP query caller."""
    try:
        token = config.hydra_token()
    except RuntimeError as exc:
        pytest.skip(_UNREACHABLE.format(target=config.HYDRA_HTTP_URL, error=exc))

    client = httpx.Client(
        base_url=config.HYDRA_HTTP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Graph-Namespace": config.HYDRA_NAMESPACE,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    caller = Http(client)
    try:
        caller.healthz()
    except httpx.HTTPError as exc:
        client.close()
        pytest.skip(_UNREACHABLE.format(target=config.HYDRA_HTTP_URL, error=exc))

    try:
        yield caller
    finally:
        client.close()


# --- Test data --------------------------------------------------------------


@pytest.fixture
def upsert_nodes(bolt: Bolt) -> Callable[[str, list[dict[str, Any]]], None]:
    """Write nodes through the one vertex-upsert form the engine executes.

    Tests build their fixtures with the real form rather than a convenience
    path, so a change that breaks the loader breaks the setup too.
    """

    def _upsert(label: str, rows: list[dict[str, Any]]) -> None:
        bolt.run(
            f"UNWIND $rows AS row MERGE (n {{id: row.vertex}}) "
            f"SET n:{label}, n.name = row.name",
            {"rows": rows},
        )

    return _upsert
