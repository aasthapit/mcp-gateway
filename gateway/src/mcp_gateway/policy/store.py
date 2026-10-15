"""Where policy snapshots come from.

:class:`MongoPolicyStore` (docs/PLAN.md section 3.3, ADR 0008):

* ``load()`` reads ``upstreams``, ``toolsets``, ``grants`` and ``meta.version`` with
  ``readConcern: majority`` (so a ``w: majority`` admin write is visible at once) and
  re-reads when the version moved underneath it.
* ``watch()`` merges two sources into one stream of :class:`ChangeEvent`:
  (a) one database change stream filtered to the four collections with
  ``fullDocument: updateLookup``; the resume token is kept in memory, resumable errors
  resume from it, ``ChangeStreamHistoryLost``/``invalidate`` drop it and trigger a full
  reload; (b) a ``GATEWAY_POLICY_POLL_SEC`` poll of ``meta.version`` that fires when the
  stored version differs from the one last loaded.

The pymongo async client is created lazily on the event loop that first uses it, because
the gateway (and its tests) may run uvicorn on a thread with its own loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern

from mcp_gateway.middleware.metrics import CHANGE_STREAM_UP, POLICY_INVALID_DOCS
from mcp_gateway.policy.snapshot import AllowAllSnapshot, PolicySnapshot, build_snapshot
from mcp_gateway.telemetry import get_logger

log = get_logger(__name__)

ChangeReason = Literal["initial", "stream", "poll", "manual"]

POLICY_COLLECTIONS: tuple[str, ...] = ("upstreams", "toolsets", "grants", "meta")
META_ID = "policy"

#: Server error codes after which the resume token is useless (reopen without it and
#: reload). 286 = ChangeStreamHistoryLost (oplog rolled past the token),
#: 280 = ChangeStreamFatalError, 136 = CappedPositionLost.
_HISTORY_LOST_CODES = frozenset({286, 280, 136})


@dataclass(frozen=True)
class ChangeEvent:
    """Something that should trigger a reload."""

    reason: ChangeReason
    collection: str | None = None
    version: int | None = None
    """``meta.version`` carried by the event when known (poll; meta documents on the
    stream). ``None`` means "reload and find out"."""
    operation: str | None = None
    """Change stream ``operationType`` (``insert``, ``update``, ``replace``, ``delete``,
    ``invalidate``, ...); ``None`` for polls and for a (re)opened stream."""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class PolicyStore(Protocol):
    async def load(self) -> PolicySnapshot: ...

    def watch(self) -> AsyncIterator[ChangeEvent]:
        """Change stream and version poll merged into one stream of events."""
        ...


class StaticPolicyStore:
    """A fixed snapshot and a watch stream that never yields (``GATEWAY_POLICY_SOURCE=static``)."""

    def __init__(self, snapshot: PolicySnapshot | None = None) -> None:
        self._snapshot = snapshot or AllowAllSnapshot()

    async def load(self) -> PolicySnapshot:
        return self._snapshot

    async def watch(self) -> AsyncIterator[ChangeEvent]:  # type: ignore[override]
        return
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


def is_history_lost(exc: BaseException) -> bool:
    """True when the change stream cannot be resumed from its token."""
    code = getattr(exc, "code", None)
    if code in _HISTORY_LOST_CODES:
        return True
    details = getattr(exc, "details", None) or {}
    return details.get("code") in _HISTORY_LOST_CODES


class MongoPolicyStore:
    def __init__(
        self,
        uri: str,
        db_name: str,
        *,
        poll_sec: float = 30.0,
        app_name: str = "mcp-gateway",
        server_selection_timeout_ms: int = 5_000,
        max_backoff_sec: float = 10.0,
        client: AsyncMongoClient[dict[str, Any]] | None = None,
    ) -> None:
        self._uri = uri
        self._db_name = db_name
        self.poll_sec = poll_sec
        self._app_name = app_name
        self._sst_ms = server_selection_timeout_ms
        self._max_backoff = max_backoff_sec
        self._client: AsyncMongoClient[dict[str, Any]] | None = client
        self._owns_client = client is None

        self.loaded_version: int | None = None
        """``meta.version`` of the last successful :meth:`load` (what the poll compares to)."""
        self.stream_up: bool = False
        self.stream_opened_at: datetime | None = None
        self.last_stream_error: str | None = None
        self.resumes: int = 0
        """Stream reopens with a resume token (after a resumable error)."""
        self.full_reopens: int = 0
        """Stream reopens without a token (first open, history lost, invalidate)."""
        self.resume_token: Any = None
        CHANGE_STREAM_UP.set(0)

    # -- connection -----------------------------------------------------------------------

    def _get_client(self) -> AsyncMongoClient[dict[str, Any]]:
        if self._client is None:
            self._client = AsyncMongoClient(
                self._uri,
                appname=self._app_name,
                serverSelectionTimeoutMS=self._sst_ms,
                connectTimeoutMS=self._sst_ms,
                retryReads=True,
            )
        return self._client

    def _db(self) -> Any:
        return self._get_client().get_database(self._db_name, read_concern=ReadConcern("majority"))

    async def ping(self) -> bool:
        await self._get_client().admin.command("ping")
        return True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
        self._client = None

    # -- load -----------------------------------------------------------------------------

    async def _read_version(self) -> int:
        doc = await self._db()["meta"].find_one({"_id": META_ID}, {"version": 1})
        return int(doc["version"]) if doc and "version" in doc else 0

    async def load(self) -> PolicySnapshot:
        db = self._db()
        for attempt in range(3):
            before = await self._read_version()
            upstreams = await db["upstreams"].find().to_list()
            toolsets = await db["toolsets"].find().to_list()
            grants = await db["grants"].find().to_list()
            after = await self._read_version()
            if after == before:
                break
            log.info("policy version moved during load; re-reading", attempt=attempt + 1)
        snapshot, report = build_snapshot(
            version=after, upstreams=upstreams, toolsets=toolsets, grants=grants
        )
        for collection, n in report.invalid.items():
            POLICY_INVALID_DOCS.labels(collection=collection).set(n)
        self.loaded_version = after
        return snapshot

    # -- watch ----------------------------------------------------------------------------

    async def watch(self) -> AsyncIterator[ChangeEvent]:  # type: ignore[override]
        queue: asyncio.Queue[ChangeEvent] = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._stream_loop(queue), name="policy-change-stream"),
            asyncio.create_task(self._poll_loop(queue), name="policy-version-poll"),
        ]
        try:
            while True:
                yield await queue.get()
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*tasks, return_exceptions=True)
            self.stream_up = False
            CHANGE_STREAM_UP.set(0)

    async def _poll_loop(self, queue: asyncio.Queue[ChangeEvent]) -> None:
        while True:
            await asyncio.sleep(self.poll_sec)
            try:
                version = await self._read_version()
            except PyMongoError as exc:
                log.warning("policy version poll failed", error=f"{type(exc).__name__}: {exc}")
                continue
            if version != self.loaded_version:
                log.info(
                    "policy version poll detected a change",
                    stored=version,
                    loaded=self.loaded_version,
                    stream_up=self.stream_up,
                )
                await queue.put(ChangeEvent(reason="poll", collection="meta", version=version))

    async def _stream_loop(self, queue: asyncio.Queue[ChangeEvent]) -> None:
        pipeline = [{"$match": {"ns.coll": {"$in": list(POLICY_COLLECTIONS)}}}]
        backoff = 0.5
        while True:
            token = self.resume_token
            try:
                stream = await self._db().watch(
                    pipeline,
                    full_document="updateLookup",
                    resume_after=token,
                    max_await_time_ms=1_000,
                )
                async with stream:
                    self._mark_stream_up(resumed=token is not None)
                    backoff = 0.5
                    if token is None:
                        # Nothing to resume from: whatever happened between the last
                        # load and now is unknown, so ask for a reload.
                        await queue.put(ChangeEvent(reason="stream", operation=None))
                    while stream.alive:
                        change = await stream.try_next()
                        self.resume_token = stream.resume_token
                        if change is None:
                            continue
                        op = change.get("operationType")
                        if op == "invalidate":
                            log.warning("policy change stream invalidated; reopening")
                            self.resume_token = None
                            break
                        coll = (change.get("ns") or {}).get("coll")
                        full = change.get("fullDocument") or {}
                        version = full.get("version") if coll == "meta" else None
                        await queue.put(
                            ChangeEvent(
                                reason="stream",
                                collection=coll,
                                version=int(version) if version is not None else None,
                                operation=op,
                            )
                        )
                    # The cursor died without an exception (invalidate or closed); loop
                    # reopens it, with the token if we still have one.
                    self._mark_stream_down("cursor closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if is_history_lost(exc):
                    log.warning("policy change stream history lost; full reload", error=error)
                    self.resume_token = None
                elif isinstance(exc, PyMongoError):
                    log.warning(
                        "policy change stream error; resuming",
                        error=error,
                        have_token=self.resume_token is not None,
                        backoff_sec=backoff,
                    )
                else:
                    log.error("policy change stream crashed; reopening", error=error)
                    self.resume_token = None
                self._mark_stream_down(error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)

    def _mark_stream_up(self, *, resumed: bool) -> None:
        if resumed:
            self.resumes += 1
        else:
            self.full_reopens += 1
        self.stream_up = True
        self.stream_opened_at = datetime.now(UTC)
        self.last_stream_error = None
        CHANGE_STREAM_UP.set(1)
        log.info(
            "policy change stream open",
            resumed=resumed,
            collections=POLICY_COLLECTIONS,
            resumes=self.resumes,
            full_reopens=self.full_reopens,
        )

    def _mark_stream_down(self, error: str) -> None:
        self.stream_up = False
        self.last_stream_error = error
        CHANGE_STREAM_UP.set(0)

    @property
    def status(self) -> dict[str, Any]:
        return {
            "change_stream_up": self.stream_up,
            "change_stream_opened_at": (
                self.stream_opened_at.isoformat() if self.stream_opened_at else None
            ),
            "change_stream_error": self.last_stream_error,
            "resumes": self.resumes,
            "full_reopens": self.full_reopens,
            "loaded_version": self.loaded_version,
            "poll_sec": self.poll_sec,
        }
