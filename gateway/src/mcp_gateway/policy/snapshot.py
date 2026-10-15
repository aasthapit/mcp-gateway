"""Immutable, in-memory view of the policy (upstreams, toolsets, grants).

Interface fixed by docs/PLAN.md section 5. A :class:`PolicySnapshot` is built once per
reload from validated documents (:func:`build_snapshot`) and never mutated; the watcher
swaps whole snapshots. ``effective_tools`` implements PLAN section 3.2:

    effective = union(allow of enabled toolsets of matching enabled grants)
              - union(deny of the same toolsets)

matched with fnmatch globs on namespaced tool names, deny-by-default (a principal that
matches no grant, or whose toolsets allow nothing, sees nothing), memoised per
``Principal.cache_key``. :class:`AllowAllSnapshot` is the dev/test stand-in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from mcp_gateway.auth.principal import Principal
from mcp_gateway.policy.matcher import GlobToolFilter, subject_matches
from mcp_gateway.policy.models import GrantDoc, ToolsetDoc, UpstreamDoc
from mcp_gateway.telemetry import get_logger
from mcp_gateway.upstreams.models import UpstreamConfig

log = get_logger(__name__)

#: Memo entries kept per snapshot before the memo is cleared (bounds memory with many
#: distinct principals; a snapshot rarely lives long enough to matter).
MEMO_MAX_ENTRIES = 10_000


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
    """Exact-name allow set (handy for tests and fixed policies)."""

    allowed: frozenset[str]

    def allows(self, namespaced_name: str) -> bool:
        return namespaced_name in self.allowed


@dataclass(frozen=True)
class PolicySnapshot:
    """Immutable snapshot of the policy collections at ``version``."""

    version: int
    loaded_at: datetime
    upstreams: Mapping[str, UpstreamConfig] = field(default_factory=dict)
    toolsets: Mapping[str, ToolsetDoc] = field(default_factory=dict)
    grants: Mapping[str, GrantDoc] = field(default_factory=dict)
    _memo: dict[str, ToolFilter] = field(
        default_factory=dict, init=False, repr=False, compare=False, hash=False
    )

    def matching_grants(self, p: Principal) -> list[str]:
        """Ids of the enabled grants whose subject selects ``p``.

        Ordered by ``priority`` (highest first) then id; the order is for display and
        audit only, set semantics do not depend on it.
        """
        hits = [g for g in self.grants.values() if g.enabled and subject_matches(g.subject, p)]
        hits.sort(key=lambda g: (-g.priority, g.id))
        return [g.id for g in hits]

    def effective_tools(self, p: Principal) -> ToolFilter:
        key = p.cache_key
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        allow: set[str] = set()
        deny: set[str] = set()
        for gid in self.matching_grants(p):
            for ts_id in self.grants[gid].toolsets:
                ts = self.toolsets.get(ts_id)
                if ts is None or not ts.enabled:
                    continue
                allow.update(ts.allow)
                deny.update(ts.deny)
        result: ToolFilter = GlobToolFilter(tuple(sorted(allow)), tuple(sorted(deny)))
        if len(self._memo) >= MEMO_MAX_ENTRIES:
            self._memo.clear()
        self._memo[key] = result
        return result

    def effective_names(self, p: Principal, names: Iterable[str]) -> list[str]:
        """Convenience for previews (``/internal/effective-tools``, WP5)."""
        f = self.effective_tools(p)
        return [n for n in names if f.allows(n)]

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.loaded_at).total_seconds()

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "loaded_at": self.loaded_at.isoformat(),
            "upstreams": len(self.upstreams),
            "toolsets": len(self.toolsets),
            "grants": len(self.grants),
        }


@dataclass(frozen=True)
class AllowAllSnapshot(PolicySnapshot):
    """Dev/test stand-in (``GATEWAY_POLICY_SOURCE=static``): everyone sees every tool."""

    version: int = 0
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def effective_tools(self, p: Principal) -> ToolFilter:
        return AllowAllFilter()

    def matching_grants(self, p: Principal) -> list[str]:
        return ["*allow-all*"]


def empty_snapshot() -> PolicySnapshot:
    """What the gateway holds before the first load: no grants, so nothing is visible."""
    return PolicySnapshot(version=-1, loaded_at=datetime.now(UTC))


@dataclass(frozen=True)
class BuildReport:
    """Documents skipped by :func:`build_snapshot` because they failed validation."""

    invalid: Mapping[str, int]
    unknown_toolsets: tuple[str, ...]

    @property
    def total_invalid(self) -> int:
        return sum(self.invalid.values())


def build_snapshot(
    *,
    version: int,
    upstreams: Iterable[Mapping[str, Any]] = (),
    toolsets: Iterable[Mapping[str, Any]] = (),
    grants: Iterable[Mapping[str, Any]] = (),
    loaded_at: datetime | None = None,
) -> tuple[PolicySnapshot, BuildReport]:
    """Validate raw documents and build a snapshot.

    One invalid document (possible after a schema change, or when the Mongo validators
    were bypassed) is logged and skipped rather than failing the reload: a stale-but-
    loaded policy beats an unloadable one. The report carries the counts for metrics.
    """
    invalid: dict[str, int] = {"upstreams": 0, "toolsets": 0, "grants": 0}

    def parse(collection: str, model: type, docs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for raw in docs:
            try:
                doc = model.model_validate(raw)
            except ValidationError as exc:
                invalid[collection] += 1
                log.error(
                    "policy document rejected",
                    collection=collection,
                    id=raw.get("_id"),
                    errors=exc.error_count(),
                    detail=str(exc).splitlines()[:6],
                )
                continue
            out[doc.id] = doc
        return out

    ups = parse("upstreams", UpstreamDoc, upstreams)
    tss = parse("toolsets", ToolsetDoc, toolsets)
    grs = parse("grants", GrantDoc, grants)

    unknown = sorted({ts for g in grs.values() for ts in g.toolsets if ts not in tss})
    if unknown:
        log.warning("grants reference unknown toolsets (ignored)", toolsets=unknown)

    snapshot = PolicySnapshot(
        version=version,
        loaded_at=loaded_at or datetime.now(UTC),
        upstreams=ups,
        toolsets=tss,
        grants=grs,
    )
    return snapshot, BuildReport(invalid=invalid, unknown_toolsets=tuple(unknown))
