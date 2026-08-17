"""Objects the API shares across its worker threads.

FastAPI runs synchronous endpoints on a thread pool, and the API caches one
client, one ingestor and one row locator for the life of the process. Anything
cached that way is used from a thread other than the one that built it, and
SQLite refuses that outright — which surfaced as `sqlite3.ProgrammingError` the
moment two questions arrived together.

The fix is a lock per connection rather than only `check_same_thread=False`:
that flag removes the guard without making concurrent use correct. These tests
drive the real objects from several threads at once, because the failure only
appears under exactly that.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tracegraph import config
from tracegraph.ids import IdRegistry, node_identity
from tracegraph.parquet_reader import RowLocator

pytestmark = pytest.mark.skipif(
    not config.locator_parquet().exists() or not config.locator_db().exists(),
    reason="needs the indexed corpus",
)


@pytest.fixture(scope="module")
def locator():
    loc = RowLocator(config.locator_parquet(), config.locator_db())
    yield loc
    loc.close()


def _sample_ids(locator: RowLocator, n: int) -> list[str]:
    rows = locator._conn.execute(
        "SELECT doc_id FROM doc_location LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows]


def test_locator_fetches_from_a_thread_that_did_not_open_it(locator):
    """The narrowest form of the bug: one other thread, one fetch."""
    doc_id = _sample_ids(locator, 1)[0]
    box: dict = {}

    def go():
        try:
            box["record"] = locator.fetch(doc_id)
        except sqlite3.ProgrammingError as exc:  # the original failure
            box["error"] = exc

    thread = threading.Thread(target=go)
    thread.start()
    thread.join()

    assert "error" not in box, f"cross-thread fetch raised {box.get('error')}"
    assert box["record"] is not None


def test_locator_survives_concurrent_fetches(locator):
    """Several threads at once, which is what the API actually does.

    A lock is what makes this correct; without one the parquet decode interleaves
    and a fetch can return another document's row, which the doc_id check inside
    `fetch` turns into a RuntimeError rather than a silent wrong answer.
    """
    doc_ids = _sample_ids(locator, 24)

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(locator.fetch, doc_ids))

    assert all(r is not None for r in records)
    assert [r["doc_id"] for r in records] == doc_ids, "a fetch returned another row"


def test_id_registry_registers_from_many_threads(tmp_path):
    """The registry is shared by the same cache and mutated on every ingest."""
    registry = IdRegistry(tmp_path / "registry.sqlite3")
    errors: list[Exception] = []

    def register(i: int) -> None:
        try:
            registry.register_many(
                [node_identity("Document", f"dsid_{i}_{j}") for j in range(20)])
        except Exception as exc:  # noqa: BLE001 - the assertion is that there are none
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(8)))

    assert not errors, f"registry raised under concurrency: {errors[:3]}"
    assert registry.count("node") == 8 * 20
