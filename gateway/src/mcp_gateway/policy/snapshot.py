"""Immutable, in-memory view of the policy (upstreams, toolsets, grants).

Interface fixed by docs/PLAN.md section 5. WP3 builds real snapshots from MongoDB with
compiled glob matchers and memoised ``effective_tools``; WP1 ships
:class:`AllowAllSnapshot` so the gateway works before policy exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from mcp_gateway.auth.principal import Principal
from mcp_gateway.upstreams.models import UpstreamConfig


@runtime_checkable
class ToolFilter(Protocol):
    """Decides whether a namespaced tool name is visible/callable for one principal."""

    def allows(self, namespaced_name: str) -> bool: ...


@dataclass(frozen=True)
class AllowAllFilter:
    def allows(self, namespaced_name: str) -> bool:
        return True


@dataclass(frozen=True)
class DenyAllFilter:
    def allows(self, namespaced_name: str) -> bool:
        return False


@dataclass(frozen=True)
class SetToolFilter:
    """Exact-name allow set (what WP3 produces after glob expansion)."""

    allowed: frozenset[str]

    def allows(self, namespaced_name: str) -> bool:
        return namespaced_name in self.allowed


@dataclass(frozen=True)
class PolicySnapshot:
    """Immutable snapshot. Subclasses override the two query methods."""

    version: int
    loaded_at: datetime
    upstreams: Mapping[str, UpstreamConfig] = field(default_factory=dict)

    def effective_tools(self, p: Principal) -> ToolFilter:
        raise NotImplementedError

    def matching_grants(self, p: Principal) -> list[str]:
        raise NotImplementedError

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.loaded_at).total_seconds()


@dataclass(frozen=True)
class AllowAllSnapshot(PolicySnapshot):
    """WP1 stand-in: every principal sees every mounted tool."""

    version: int = 0
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def effective_tools(self, p: Principal) -> ToolFilter:
        return AllowAllFilter()

    def matching_grants(self, p: Principal) -> list[str]:
        return ["*allow-all*"]
