"""MongoPolicyStore pieces that need no server (the live behaviour is in integration)."""

from __future__ import annotations

from pymongo.errors import OperationFailure, PyMongoError

from mcp_gateway.policy.store import (
    POLICY_COLLECTIONS,
    MongoPolicyStore,
    StaticPolicyStore,
    is_history_lost,
)


def test_history_lost_classification() -> None:
    assert is_history_lost(OperationFailure("history lost", code=286))
    assert is_history_lost(OperationFailure("fatal", code=280))
    assert is_history_lost(OperationFailure("capped", code=136))
    assert not is_history_lost(OperationFailure("interrupted", code=11602))
    assert not is_history_lost(PyMongoError("network"))
    assert not is_history_lost(RuntimeError("x"))


def test_store_defaults_and_status() -> None:
    s = MongoPolicyStore("mongodb://localhost:1/?directConnection=true", "db", poll_sec=7)
    assert s.poll_sec == 7 and s.loaded_version is None and not s.stream_up
    assert s.status["change_stream_up"] is False and s.status["poll_sec"] == 7
    assert POLICY_COLLECTIONS == ("upstreams", "toolsets", "grants", "meta")


async def test_static_store_watch_never_yields() -> None:
    s = StaticPolicyStore()
    assert [e async for e in s.watch()] == []
    assert (await s.load()).version == 0
    await s.close()
