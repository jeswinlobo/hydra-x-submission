"""A dead pooled connection is retried; a rejected query is not.

The server keeps one client for the life of the process, so its Bolt
connections sit idle between questions. An idle connection can be dead at the
far end without the pool knowing, and the first read on it raised
`ServiceUnavailable` straight out to the caller — the status bar reported the
graph unreachable against a database that was running fine.

The retry has to be narrow. Replaying a statement the engine rejected would
fail again identically and bury the reason, so only the transport failures are
caught.
"""

from __future__ import annotations

import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired

from tracegraph.hydra_client import _TRANSIENT_RETRIES, HydraClient


@pytest.fixture
def client(monkeypatch) -> HydraClient:
    """A client with no driver behind it: only `_bolt_run`'s retry is exercised."""
    instance = HydraClient.__new__(HydraClient)
    monkeypatch.setattr("tracegraph.hydra_client.time.sleep", lambda _: None)
    return instance


def _failing(client, exc: Exception, succeed_on: int):
    """Replace the single-shot run with one that fails until the nth attempt."""
    calls = {"n": 0}

    def run(cypher, params, *, write):
        calls["n"] += 1
        if calls["n"] < succeed_on:
            raise exc
        return [{"n": calls["n"]}]

    client._bolt_once = run
    return calls


@pytest.mark.parametrize("exc", [
    ServiceUnavailable("defunct connection"),
    SessionExpired("session expired"),
])
def test_transient_transport_failure_is_retried(client, exc):
    calls = _failing(client, exc, succeed_on=2)
    rows = client.bolt_read("RETURN 1")
    assert rows == [{"n": 2}]
    assert calls["n"] == 2, "the statement should have run twice"


def test_retries_are_bounded(client):
    calls = _failing(client, ServiceUnavailable("still dead"), succeed_on=99)
    with pytest.raises(ServiceUnavailable):
        client.bolt_read("RETURN 1")
    assert calls["n"] == _TRANSIENT_RETRIES + 1, "a real outage must not stall"


def test_a_rejected_query_is_not_retried(client):
    """Replaying a syntax error hides the error behind a delay."""
    calls = _failing(client, ClientError("invalid syntax"), succeed_on=99)
    with pytest.raises(ClientError):
        client.bolt_read("MATCH (")
    assert calls["n"] == 1, "the engine's rejection is final"


def test_writes_are_retried_too(client):
    """Safe here specifically: every write is a MERGE on a deterministic id.

    Replaying one converges on the same graph rather than duplicating anything,
    which is the property the loader's checkpoints already depend on.
    """
    calls = _failing(client, ServiceUnavailable("defunct"), succeed_on=2)
    assert client.bolt_write("MERGE (n:X {id: 1})") == [{"n": 2}]
    assert calls["n"] == 2
