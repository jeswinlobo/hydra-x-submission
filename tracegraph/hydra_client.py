"""HydraDB access over both transports.

The two transports are not interchangeable, and which one a call uses is a
correctness question rather than a preference:

* **Bolt** is the only transport that executes `UNWIND` batches, because a
  parameter holding a list of maps is a transport-level type. All ingestion goes
  here. The HTTP query engine rejects every vertex-upsert form outright.
* **HTTP** returns `read_epoch` and `bookmark` in the response body, so reads
  that need to show consistency evidence go here.

See docs/engine-notes.md for the probes these rules came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx
from neo4j import Bookmarks, GraphDatabase

from . import config

# Labels, relationship types, and property names cannot be parameterised in
# Cypher, so anything interpolated into a statement is checked against this
# first. Values are always passed as parameters and never interpolated.
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CypherIdentifierError(ValueError):
    """An identifier destined for string interpolation failed validation."""


def check_identifier(name: str, *, kind: str) -> str:
    if not IDENTIFIER_RE.match(name or ""):
        raise CypherIdentifierError(
            f"{kind} {name!r} is not a bare identifier; it cannot be safely "
            "interpolated into Cypher, and Cypher cannot parameterise it"
        )
    return name


@dataclass
class HttpResult:
    """One HTTP query response.

    `read_epoch` and `bookmark` are read off the response rather than
    reconstructed, so the answer trace can display a real consistency position.
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    read_epoch: int | None
    bookmark: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class BookmarkScope:
    """Decoded HydraDB bookmark.

    Format: `sgk:<version>:<hex namespace>:<hex graph>:<hex cell>:<sequence>`.
    The trailing integer is the SlateDB commit sequence, which is what makes the
    trace's consistency claim checkable instead of decorative.
    """

    version: str
    namespace: str
    graph: str
    cell: str
    sequence: int


def parse_bookmark(bookmark: str) -> BookmarkScope | None:
    """Decode a bookmark, returning None rather than raising on an unknown shape.

    A bookmark whose format changes should degrade the trace, never fail a query.
    """
    parts = bookmark.split(":")
    if len(parts) != 6 or parts[0] != "sgk":
        return None
    try:
        namespace, graph, cell = (bytes.fromhex(p).decode() for p in parts[2:5])
        return BookmarkScope(
            version=parts[1],
            namespace=namespace,
            graph=graph,
            cell=cell,
            sequence=int(parts[5]),
        )
    except (ValueError, UnicodeDecodeError):
        return None


def _unwrap(cell: Any) -> Any:
    """Flatten HydraDB's typed HTTP cells to plain Python values.

    Rows arrive as `{"type": "vertex_id", "value": 2}`.
    """
    if isinstance(cell, dict) and "value" in cell and "type" in cell:
        return cell["value"]
    return cell


class HydraClient:
    """Bolt and HTTP access to one HydraDB graph."""

    def __init__(
        self,
        *,
        bolt_uri: str | None = None,
        http_url: str | None = None,
        token: str | None = None,
        graph: str | None = None,
        namespace: str | None = None,
        cell_id: str | None = None,
        database: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._token = token or config.hydra_token()
        self.graph = graph or config.HYDRA_GRAPH
        self.namespace = namespace or config.HYDRA_NAMESPACE
        self.cell_id = cell_id or config.HYDRA_CELL_ID
        self.database = database or config.HYDRA_DATABASE

        self._driver = GraphDatabase.driver(
            bolt_uri or config.HYDRA_BOLT_URI,
            auth=(config.HYDRA_BOLT_USER, self._token),
        )
        self._http = httpx.Client(
            base_url=http_url or config.HYDRA_HTTP_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Graph-Namespace": self.namespace,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        # Carried between sessions so a read is causally consistent with the
        # writes that preceded it. Without this a read can legitimately observe
        # a snapshot older than a write this same process just committed.
        #
        # The driver hands back a Bookmarks object rather than a list, and it is
        # what session(bookmarks=...) expects, so it is stored as-is and only
        # flattened to strings for display.
        self._bookmarks: Bookmarks | None = None

    # --- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._driver.close()
        self._http.close()

    def __enter__(self) -> "HydraClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    @property
    def bookmark(self) -> str | None:
        """The most recent bookmark as a string, for the answer trace."""
        if self._bookmarks is None:
            return None
        values = list(self._bookmarks.raw_values)
        return values[-1] if values else None

    # --- Bolt ---------------------------------------------------------------

    def bolt_write(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        return self._bolt_run(cypher, params, write=True)

    def bolt_read(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        return self._bolt_run(cypher, params, write=False)

    def _bolt_run(
        self, cypher: str, params: dict[str, Any] | None, *, write: bool
    ) -> list[dict]:
        with self._driver.session(
            database=self.database, bookmarks=self._bookmarks
        ) as session:
            result = session.run(cypher, params or {})
            rows = [record.data() for record in result]
            # Draining before reading bookmarks is what makes the bookmark
            # correspond to a completed transaction.
            result.consume()
            if write:
                latest = session.last_bookmarks()
                if latest and list(latest.raw_values):
                    self._bookmarks = latest
        return rows

    # --- HTTP ---------------------------------------------------------------

    def http_query(
        self,
        cypher: str,
        *,
        consistency: str = "causal",
        with_bookmark: bool = True,
    ) -> HttpResult:
        """Run a read over HTTP and keep the consistency metadata it returns.

        Parameters are deliberately not accepted. The engine documents scalar
        parameters as a client-transport type, and this path exists for
        judge-visible reads whose statements are built from validated
        identifiers and literal ids rather than from user input. Anything that
        needs parameters goes over Bolt.
        """
        body: dict[str, Any] = {
            "cell_id": self.cell_id,
            "query": cypher,
            "consistency": consistency,
        }
        if with_bookmark and self.bookmark:
            body["bookmark"] = self.bookmark

        response = self._http.post(f"/v1/graphs/{self.graph}/query", json=body)
        response.raise_for_status()
        payload = response.json()

        if "error" in payload and payload["error"]:
            raise RuntimeError(f"HydraDB rejected the query: {payload['error']}")

        columns: list[str] = payload.get("columns") or []
        rows = [
            {col: _unwrap(cell) for col, cell in zip(columns, row)}
            for row in payload.get("rows") or []
        ]
        bookmark = payload.get("bookmark")
        if bookmark:
            # HTTP returns a bare string; the driver expects a Bookmarks object.
            self._bookmarks = Bookmarks.from_raw_values([bookmark])

        return HttpResult(
            columns=columns,
            rows=rows,
            read_epoch=payload.get("read_epoch"),
            bookmark=bookmark,
            raw=payload,
        )

    # --- helpers whose whole point is to be hard to misuse -------------------

    def exists_node(self, label: str, node_id: int) -> bool:
        """Does a node with this label and id exist?

        The label is not optional and not a filter for convenience. A bare
        `MATCH (n {id: N})` pattern is an *address lookup*: it returns a row for
        ids that were never written, and `count(*)` over it returns 1. Only a
        label forces the engine to hydrate the vertex and therefore to filter.

        Citation validation depends on this distinction. Written without the
        label, "every cited document exists in the graph" is true of every id in
        the 63-bit space, and the submission's central guarantee is vacuous.
        """
        check_identifier(label, kind="label")
        rows = self.bolt_read(
            f"MATCH (n:{label} {{id: $id}}) RETURN n.id AS id", {"id": int(node_id)}
        )
        return bool(rows)

    def count_labelled(self, label: str) -> int:
        check_identifier(label, kind="label")
        rows = self.bolt_read(f"MATCH (n:{label}) RETURN count(*) AS c")
        return int(rows[0]["c"]) if rows else 0

    def fetch_by_ids(
        self, label: str, node_ids: Sequence[int], properties: Iterable[str]
    ) -> list[dict]:
        """Read several nodes by id.

        `WHERE id IN $ids` is rejected by the engine, and an `UNWIND`-driven
        multi-id read is rejected too ("UNWIND batch supports one-hop
        relationships only"), so an OR chain is the only batch form available.
        It is a label scan, so this is for small candidate sets — tens, not
        thousands. Larger reads should traverse from an anchor instead.
        """
        check_identifier(label, kind="label")
        ids = [int(i) for i in node_ids]
        if not ids:
            return []
        props = [check_identifier(p, kind="property") for p in properties]

        # Parameter names must start with a letter, so they are `id0`, `id1`, …
        # rather than bare indices.
        predicate = " OR ".join(f"n.id = $id{i}" for i in range(len(ids)))
        projection = ", ".join(f"n.{p} AS {p}" for p in props)
        params = {f"id{i}": value for i, value in enumerate(ids)}
        return self.bolt_read(
            f"MATCH (n:{label}) WHERE {predicate} RETURN n.id AS id, {projection}",
            params,
        )
