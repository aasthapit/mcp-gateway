"""PolicyWatcher with a fake store: debounce, swap, notification, metrics, readiness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged

from mcp_gateway.auth.principal import ANONYMOUS
from mcp_gateway.health import Readiness
from mcp_gateway.middleware.metrics import POLICY_RELOAD_FAILURES, POLICY_RELOADS, POLICY_VERSION
from mcp_gateway.policy.snapshot import PolicySnapshot, build_snapshot
from mcp_gateway.policy.store import ChangeEvent
from mcp_gateway.policy.watcher import PolicyHolder, PolicyWatcher


def snapshot(version: int, allow: list[str]) -> PolicySnapshot:
    snap, _ = build_snapshot(
        version=version,
        toolsets=[{"_id": "t", "enabled": True, "allow": allow}],
        grants=[{"_id": "g", "enabled": True, "subject": {"kind": "anonymous"}, "toolsets": ["t"]}],
    )
    return snap


class FakeStore:
    def __init__(self) -> None:
        self.events: asyncio.Queue[ChangeEvent] = asyncio.Queue()
        self.version = 1
        self.loads = 0
        self.fail_next = 0
        self.stream_up = True
        self.last_stream_error: str | None = None
        self.closed = False

    async def load(self) -> PolicySnapshot:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("mongo down")
        self.loads += 1
        return snapshot(self.version, [f"tool_{self.version}"])

    async def watch(self) -> AsyncIterator[ChangeEvent]:
        while True:
            yield await self.events.get()

    async def close(self) -> None:
        self.closed = True

    @property
    def status(self) -> dict:
        return {"change_stream_up": self.stream_up}


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def counter(reason: str) -> float:
    return POLICY_RELOADS.labels(reason=reason)._value.get()


async def test_initial_load_swap_and_readiness() -> None:
    store, holder, readiness = FakeStore(), PolicyHolder(), Readiness()
    assert not holder.loaded and not holder.snapshot.effective_tools(ANONYMOUS).allows("tool_1")
    w = PolicyWatcher(store, holder, readiness=readiness, debounce_ms=20)
    initial_before = counter("initial")
    await w.start()
    try:
        assert holder.loaded and holder.snapshot.version == 1
        assert holder.snapshot.effective_tools(ANONYMOUS).allows("tool_1")
        assert POLICY_VERSION._value.get() == 1
        assert counter("initial") == initial_before + 1
        readiness.started = True
        ok, detail = await readiness.evaluate()
        assert ok and detail["checks"]["policy"]["ok"]
        assert detail["checks"]["policy"]["version"] == 1
        assert detail["checks"]["policy"]["policy_age_seconds"] >= 0
        assert detail["checks"]["policy_stream"]["critical"] is False
    finally:
        await w.stop()
    assert store.closed and not w.running


async def test_debounce_collapses_a_burst_and_notifies_listeners() -> None:
    store, holder, bus = FakeStore(), PolicyHolder(), InMemorySubscriptionBus()
    received: list = []
    bus.subscribe(received.append)
    applied: list[int] = []

    async def on_reload(snap: PolicySnapshot) -> None:
        applied.append(snap.version)

    w = PolicyWatcher(store, holder, bus=bus, on_reload=on_reload, debounce_ms=100)
    stream_before = counter("stream")
    await w.start()
    try:
        store.version = 2
        for coll in ("toolsets", "grants", "meta"):
            await store.events.put(ChangeEvent(reason="stream", collection=coll))
        await wait_for(lambda: holder.snapshot.version == 2)
        await asyncio.sleep(0.25)  # longer than the debounce: no further reload
        assert store.loads == 2  # initial + one for the burst
        assert counter("stream") == stream_before + 1
        assert w.events_seen == 3
        assert applied == [1, 2]
        assert any(isinstance(e, ToolsListChanged) for e in received)
        assert holder.snapshot.effective_tools(ANONYMOUS).allows("tool_2")
        assert not holder.snapshot.effective_tools(ANONYMOUS).allows("tool_1")

        # a poll event alone reloads with reason=poll
        poll_before = counter("poll")
        store.version = 3
        await store.events.put(ChangeEvent(reason="poll", collection="meta", version=3))
        await wait_for(lambda: holder.snapshot.version == 3)
        assert counter("poll") == poll_before + 1
    finally:
        await w.stop()


async def test_failed_reload_keeps_previous_snapshot_and_stays_ready() -> None:
    store, holder, readiness = FakeStore(), PolicyHolder(), Readiness()
    w = PolicyWatcher(store, holder, readiness=readiness, debounce_ms=10)
    await w.start()
    readiness.started = True
    try:
        failures_before = POLICY_RELOAD_FAILURES._value.get()
        store.fail_next = 1
        store.version = 2
        await store.events.put(ChangeEvent(reason="stream", collection="toolsets"))
        await wait_for(lambda: w.last_error is not None)
        assert holder.snapshot.version == 1
        assert POLICY_RELOAD_FAILURES._value.get() == failures_before + 1
        ok, detail = await readiness.evaluate()
        assert ok and detail["checks"]["policy"]["last_error"].startswith("ConnectionError")
        # next event succeeds
        await store.events.put(ChangeEvent(reason="stream", collection="toolsets"))
        await wait_for(lambda: holder.snapshot.version == 2)
        assert w.last_error is None
    finally:
        await w.stop()


async def test_initial_load_failure_retries_in_background() -> None:
    store, holder, readiness = FakeStore(), PolicyHolder(), Readiness()
    store.fail_next = 2
    w = PolicyWatcher(store, holder, readiness=readiness, debounce_ms=10, retry_min_sec=0.05)
    await w.start()
    readiness.started = True
    try:
        ok, detail = await readiness.evaluate()
        assert not ok and detail["checks"]["policy"]["ok"] is False
        assert not holder.snapshot.effective_tools(ANONYMOUS).allows("tool_1")  # deny-all meanwhile
        await wait_for(lambda: holder.loaded, timeout=3)
        ok, _ = await readiness.evaluate()
        assert ok
    finally:
        await w.stop()


async def test_watch_stream_ending_restarts() -> None:
    class EndingStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.watches = 0

        async def watch(self) -> AsyncIterator[ChangeEvent]:
            self.watches += 1
            if self.watches == 1:
                return  # ends immediately once
            while True:
                yield await self.events.get()

    store, holder = EndingStore(), PolicyHolder()
    w = PolicyWatcher(store, holder, debounce_ms=10, retry_min_sec=0.05)
    await w.start()
    try:
        await wait_for(lambda: store.watches == 2)
        store.version = 5
        await store.events.put(ChangeEvent(reason="stream"))
        await wait_for(lambda: holder.snapshot.version == 5)
    finally:
        await w.stop()


def test_holder_swap_returns_previous() -> None:
    holder = PolicyHolder()
    first = holder.snapshot
    assert first.version == -1 and not holder.loaded
    prev = holder.swap(snapshot(4, ["a"]))
    assert prev is first and holder.loaded and holder.snapshot.version == 4
    assert isinstance(holder.swapped_at, datetime) and holder.swapped_at.tzinfo is UTC


@pytest.mark.parametrize("reason", ["initial", "stream", "poll"])
def test_change_event_reasons(reason: str) -> None:
    ev = ChangeEvent(reason=reason)  # type: ignore[arg-type]
    assert ev.reason == reason and ev.collection is None and ev.at.tzinfo is UTC
