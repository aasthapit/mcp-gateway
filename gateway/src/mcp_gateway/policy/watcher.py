"""Hot reload: keeps the in-memory policy snapshot current (docs/PLAN.md 3.3, ADR 0008).

:class:`PolicyHolder` is the one mutable cell: middleware reads ``holder.snapshot`` on
every request (a plain attribute read, atomic under the GIL) and the watcher swaps whole
immutable snapshots into it.

:class:`PolicyWatcher` runs one task per process:

* initial load before the pod is ready (retried in the background if Mongo is down;
  the readiness probe stays red until a snapshot is loaded),
* then every :class:`ChangeEvent` from ``store.watch()`` is debounced (250 ms) into one
  full reload, an atomic swap, ``registry.apply`` for new upstreams, and a
  ``ToolsListChanged`` publish on the subscription bus so this pod's open
  ``subscriptions/listen`` streams get ``notifications/tools/list_changed`` (ADR 0003),
* metrics: ``gateway_policy_version``, ``gateway_policy_age_seconds`` (live gauge),
  ``gateway_policy_reloads_total{reason}``, ``gateway_policy_reload_failures_total``;
  ``gateway_change_stream_up`` is owned by the store,
* readiness: ``policy`` (critical: a snapshot is loaded; reports ``policy_age_seconds``
  and stays green on a stale snapshot) and ``policy_stream`` (non-critical: the change
  stream state, for operators; never flips readiness and never touches liveness).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.subscriptions import SubscriptionBus, ToolsListChanged

from mcp_gateway.health import Readiness
from mcp_gateway.middleware.metrics import (
    POLICY_AGE,
    POLICY_RELOAD_FAILURES,
    POLICY_RELOADS,
    POLICY_VERSION,
)
from mcp_gateway.policy.snapshot import PolicySnapshot, empty_snapshot
from mcp_gateway.policy.store import ChangeEvent, ChangeReason, PolicyStore
from mcp_gateway.telemetry import get_logger

log = get_logger(__name__)

ReloadHook = Callable[[PolicySnapshot], Awaitable[None]]


class PolicyHolder:
    """Atomic cell holding the current snapshot. Starts empty (deny-all) and unloaded."""

    def __init__(self, initial: PolicySnapshot | None = None) -> None:
        self._snapshot: PolicySnapshot = initial if initial is not None else empty_snapshot()
        self.loaded: bool = initial is not None
        self.swapped_at: datetime | None = None

    @property
    def snapshot(self) -> PolicySnapshot:
        return self._snapshot

    def get(self) -> PolicySnapshot:
        return self._snapshot

    def swap(self, snapshot: PolicySnapshot) -> PolicySnapshot:
        """Replace the snapshot; returns the previous one."""
        previous = self._snapshot
        self._snapshot = snapshot
        self.loaded = True
        self.swapped_at = datetime.now(UTC)
        POLICY_VERSION.set(snapshot.version)
        return previous


class PolicyWatcher:
    def __init__(
        self,
        store: PolicyStore,
        holder: PolicyHolder,
        *,
        bus: SubscriptionBus | None = None,
        on_reload: ReloadHook | None = None,
        readiness: Readiness | None = None,
        debounce_ms: int = 250,
        retry_min_sec: float = 1.0,
        retry_max_sec: float = 15.0,
    ) -> None:
        self.store = store
        self.holder = holder
        self.bus = bus
        self.on_reload = on_reload
        self.readiness = readiness
        self.debounce = debounce_ms / 1000
        self._retry_min = retry_min_sec
        self._retry_max = retry_max_sec
        self._task: asyncio.Task[None] | None = None
        self._reload_lock = asyncio.Lock()
        self.reloads = 0
        self.last_reload_at: datetime | None = None
        self.last_reload_reason: str | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.events_seen = 0

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        """Initial load (one attempt here, retried by the task), then start watching."""
        self._register_probes()
        POLICY_AGE.set_function(lambda: self.holder.snapshot.age_seconds)
        try:
            await self.reload("initial")
        except Exception as exc:
            log.error(
                "initial policy load failed; gateway stays not-ready and retries",
                error=f"{type(exc).__name__}: {exc}",
            )
        self._task = asyncio.create_task(self._run(), name="policy-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None
        close = getattr(self.store, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- reload ---------------------------------------------------------------------------

    async def reload(self, reason: ChangeReason) -> PolicySnapshot:
        """Load a fresh snapshot, swap it in, notify. Raises if the load fails."""
        async with self._reload_lock:
            try:
                snapshot = await self.store.load()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_error_at = datetime.now(UTC)
                POLICY_RELOAD_FAILURES.inc()
                raise
            previous = self.holder.swap(snapshot)
            self.reloads += 1
            self.last_reload_at = datetime.now(UTC)
            self.last_reload_reason = reason
            self.last_error = None
            POLICY_RELOADS.labels(reason=reason).inc()
            log.info(
                "policy snapshot swapped",
                reason=reason,
                previous_version=previous.version,
                **snapshot.summary,
            )
            if self.on_reload is not None:
                try:
                    await self.on_reload(snapshot)
                except Exception as exc:  # the snapshot is live already; hooks must not undo that
                    log.error("policy reload hook failed", error=f"{type(exc).__name__}: {exc}")
            if self.bus is not None:
                try:
                    await self.bus.publish(ToolsListChanged())
                except Exception as exc:
                    log.error("tools/list_changed publish failed", error=str(exc))
            return snapshot

    # -- task -----------------------------------------------------------------------------

    async def _run(self) -> None:
        backoff = self._retry_min
        while not self.holder.loaded:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._retry_max)
            with contextlib.suppress(Exception):
                await self.reload("initial")
        backoff = self._retry_min
        while True:
            try:
                await self._watch_once()
                log.warning("policy watch stream ended; restarting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "policy watcher crashed; restarting", error=f"{type(exc).__name__}: {exc}"
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._retry_max)

    async def _watch_once(self) -> None:
        queue: asyncio.Queue[ChangeEvent | None] = asyncio.Queue()
        events = self.store.watch()

        async def pump() -> None:
            try:
                async for ev in events:
                    await queue.put(ev)
            finally:
                await queue.put(None)

        pump_task = asyncio.create_task(pump(), name="policy-watch-pump")
        try:
            while True:
                first = await queue.get()
                if first is None:
                    return
                reasons = {first.reason}
                collections = {first.collection}
                n = 1
                while True:  # debounce: swallow the burst, reload once
                    try:
                        more = await asyncio.wait_for(queue.get(), self.debounce)
                    except TimeoutError:
                        break
                    if more is None:
                        return
                    reasons.add(more.reason)
                    collections.add(more.collection)
                    n += 1
                self.events_seen += n
                reason: ChangeReason = "stream" if "stream" in reasons else "poll"
                log.info(
                    "policy change detected",
                    reason=reason,
                    events=n,
                    collections=sorted(c for c in collections if c),
                    version_hint=first.version,
                )
                try:
                    await self.reload(reason)
                except Exception as exc:
                    log.error(
                        "policy reload failed; keeping previous snapshot",
                        error=f"{type(exc).__name__}: {exc}",
                        version=self.holder.snapshot.version,
                    )
        finally:
            pump_task.cancel()
            with contextlib.suppress(BaseException):
                await pump_task
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(BaseException):
                    await aclose()

    # -- health ---------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        snap = self.holder.snapshot
        info: dict[str, Any] = {
            "loaded": self.holder.loaded,
            "version": snap.version,
            "policy_age_seconds": round(snap.age_seconds, 3) if self.holder.loaded else None,
            "reloads": self.reloads,
            "last_reload_at": self.last_reload_at.isoformat() if self.last_reload_at else None,
            "last_reload_reason": self.last_reload_reason,
            "last_error": self.last_error,
            "watching": self.running,
        }
        store_status = getattr(self.store, "status", None)
        if isinstance(store_status, dict):
            info.update(store_status)
        return info

    def _register_probes(self) -> None:
        if self.readiness is None:
            return

        def policy_loaded() -> tuple[bool, dict[str, Any]]:
            return self.holder.loaded, self.status()

        def stream_state() -> tuple[bool, dict[str, Any]]:
            # Informational: a down change stream degrades propagation latency to the
            # poll interval, it does not make the pod unable to serve.
            up = getattr(self.store, "stream_up", True)
            return bool(up), {
                "change_stream_up": bool(up),
                "error": getattr(self.store, "last_stream_error", None),
            }

        self.readiness.register("policy", policy_loaded, critical=True)
        self.readiness.register("policy_stream", stream_state, critical=False)
