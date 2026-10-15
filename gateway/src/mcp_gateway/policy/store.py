"""Where policy snapshots come from. WP3 implements the MongoDB store with change streams."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from mcp_gateway.policy.snapshot import AllowAllSnapshot, PolicySnapshot


@dataclass(frozen=True)
class ChangeEvent:
    """Something that should trigger a reload."""

    kind: Literal["change_stream", "poll", "resume", "initial"]
    collection: str | None = None
    version: int | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class PolicyStore(Protocol):
    async def load(self) -> PolicySnapshot: ...

    def watch(self) -> AsyncIterator[ChangeEvent]:
        """Change stream and version poll merged into one stream of events."""
        ...


class StaticPolicyStore:
    """WP1: a fixed snapshot and a watch stream that never yields."""

    def __init__(self, snapshot: PolicySnapshot | None = None) -> None:
        self._snapshot = snapshot or AllowAllSnapshot()

    async def load(self) -> PolicySnapshot:
        return self._snapshot

    async def watch(self) -> AsyncIterator[ChangeEvent]:  # type: ignore[override]
        return
        yield  # pragma: no cover - makes this an async generator
