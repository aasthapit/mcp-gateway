"""Mounts upstream MCP servers on the gateway as namespaced FastMCP proxies.

Interface fixed by docs/PLAN.md section 5. :meth:`UpstreamRegistry.apply` reconciles the
desired set (``PolicySnapshot.upstreams`` or, until WP3 lands, the loader in
``upstreams/source.py``) with what is mounted:

* new or newly enabled upstream  -> build proxy (``upstreams/proxy.py``) and ``mcp.mount``;
* disabled or removed upstream   -> ``mcp.providers.remove(provider)`` (ADR 0005);
* changed config                 -> unmount + mount.

An upstream that cannot be mounted (secret missing, stdio in prod, unsupported auth,
build error) is kept as ``degraded`` with a reason: it shows up in ``health()`` and
``/readyz`` detail, counts in ``gateway_upstream_errors_total`` and is retried on the next
``apply``. One bad upstream never blocks the others and never crashes the process.

Health comes from a background probe (``tools/list`` on a fresh short-lived client every
``GATEWAY_UPSTREAM_PROBE_SEC``). A failing probe marks the upstream ``down`` and
``gateway_upstream_up{upstream}`` 0; the proxy stays mounted so ``tools/list`` keeps
working for the others (``provider_error_strategy="warn"``) and calls fail on the
upstream's connect timeout with FastMCP's ``Client failed to connect`` error.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
from fastmcp import FastMCP

from mcp_gateway.middleware.metrics import (
    UPSTREAM_ERRORS,
    UPSTREAM_PROBE_SECONDS,
    UPSTREAM_UP,
    UPSTREAMS_MOUNTED,
)
from mcp_gateway.telemetry import get_logger
from mcp_gateway.upstreams.models import CatalogTool, UpstreamConfig, UpstreamHealth
from mcp_gateway.upstreams.proxy import BuiltProxy, UnsupportedUpstream, build_proxy
from mcp_gateway.upstreams.secrets import SecretMissing, resolve_secret
from mcp_gateway.upstreams.source import static_to_config

if TYPE_CHECKING:
    from mcp_gateway.policy.snapshot import PolicySnapshot
    from mcp_gateway.settings import Settings

log = get_logger(__name__)


@dataclass
class MountedUpstream:
    """What the registry holds per configured upstream id."""

    config: UpstreamConfig
    health: UpstreamHealth
    built: BuiltProxy | None = None
    """``None`` when degraded (configured but not mounted)."""
    provider: Any = None
    """The entry ``FastMCP.mount`` appended to ``mcp.providers``; removed on unmount."""
    mounted_at: datetime | None = None
    generation: int = field(default=0)
    """Bumped on every (re)mount so a late probe result for an old mount is dropped."""

    @property
    def is_mounted(self) -> bool:
        return self.built is not None and self.provider is not None


class UpstreamRegistry:
    """Owns the set of mounted upstreams on a FastMCP server."""

    def __init__(self, mcp: FastMCP, settings: Settings | None = None) -> None:
        self._mcp = mcp
        self._settings = settings
        self._mounted: dict[str, MountedUpstream] = {}
        self._lock = asyncio.Lock()
        self._probe_task: asyncio.Task[None] | None = None
        self._probe_runs = 0

    # -- settings-derived knobs -----------------------------------------------------------

    @property
    def _is_prod(self) -> bool:
        return bool(self._settings and self._settings.is_prod)

    @property
    def _secrets_dir(self) -> str:
        return self._settings.upstream_secrets_dir if self._settings else "/nonexistent"

    @property
    def _probe_interval(self) -> float:
        return self._settings.upstream_probe_sec if self._settings else 30.0

    @property
    def _probe_timeout(self) -> float:
        return self._settings.upstream_probe_timeout_sec if self._settings else 5.0

    # -- WP1 compatibility ----------------------------------------------------------------

    def mount_static(self, settings: Settings) -> None:
        for static in settings.static_upstreams:
            self.mount(static_to_config(static))

    def mount(self, cfg: UpstreamConfig) -> MountedUpstream:
        """Mount one upstream now (synchronous; shared clients connect lazily).

        Degraded outcomes are recorded, not raised.
        """
        existing = self._mounted.get(cfg.id)
        if existing is not None and existing.is_mounted:
            log.debug("upstream already mounted", upstream=cfg.id)
            return existing
        entry = self._mount(cfg)
        self._mounted[cfg.id] = entry
        self._update_mounted_gauge()
        return entry

    # -- Section 5 interface --------------------------------------------------------------

    async def apply(self, snapshot: PolicySnapshot) -> None:
        """Reconcile with ``snapshot.upstreams`` (WP3 calls this after every reload)."""
        await self.apply_configs(snapshot.upstreams)

    async def apply_configs(
        self, configs: Mapping[str, UpstreamConfig] | Iterable[UpstreamConfig]
    ) -> Mapping[str, UpstreamHealth]:
        """Mount new/enabled, unmount disabled/removed, re-mount changed. Returns health."""
        desired = {
            cfg.id: cfg
            for cfg in (configs.values() if isinstance(configs, Mapping) else configs)
            if cfg.enabled
        }
        async with self._lock:
            for upstream_id in [u for u in self._mounted if u not in desired]:
                await self._unmount(upstream_id, reason="removed or disabled")
            for upstream_id, cfg in desired.items():
                current = self._mounted.get(upstream_id)
                if current is None:
                    self._mounted[upstream_id] = self._mount(cfg)
                elif not current.is_mounted:
                    # Degraded earlier (e.g. secret missing): retry with the new config.
                    self._mounted[upstream_id] = self._mount(cfg)
                elif not current.config.same_mount_as(cfg):
                    await self._unmount(upstream_id, reason="config changed")
                    self._mounted[upstream_id] = self._mount(
                        cfg, previous_generation=current.generation
                    )
            self._update_mounted_gauge()
        return self.health()

    async def catalog(self) -> list[CatalogTool]:
        """Every tool across mounted upstreams, namespaced, for ``/internal/catalog``."""
        out: list[CatalogTool] = []
        for upstream_id, entry in list(self._mounted.items()):
            if entry.built is None:
                continue
            try:
                tools = await entry.built.proxy.list_tools()
            except Exception as exc:
                UPSTREAM_ERRORS.labels(upstream_id, "catalog_failed").inc()
                log.warning("catalog: upstream unavailable", upstream=upstream_id, error=str(exc))
                continue
            out.extend(
                CatalogTool(
                    namespaced_name=f"{upstream_id}_{t.name}",
                    upstream=upstream_id,
                    name=t.name,
                    description=t.description,
                    input_schema=dict(t.parameters) if t.parameters else None,
                )
                for t in tools
            )
        return out

    def health(self) -> Mapping[str, UpstreamHealth]:
        return {uid: entry.health for uid, entry in self._mounted.items()}

    # -- readiness --------------------------------------------------------------------------

    def readiness_probe(self) -> tuple[bool, dict[str, Any]]:
        """Ready when at least one upstream is mounted; a dead-but-mounted one is fine."""
        mounted = sorted(u for u, e in self._mounted.items() if e.is_mounted)
        degraded = {u: e.health.reason for u, e in self._mounted.items() if not e.is_mounted}
        down = sorted(u for u, e in self._mounted.items() if e.health.status == "down")
        info: dict[str, Any] = {"mounted": mounted, "down": down, "degraded": degraded}
        return bool(mounted), info

    # -- helpers used by middleware -----------------------------------------------------------

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(u for u, e in self._mounted.items() if e.is_mounted)

    @property
    def configs(self) -> Mapping[str, UpstreamConfig]:
        return {u: e.config for u, e in self._mounted.items()}

    @property
    def mounted(self) -> Mapping[str, MountedUpstream]:
        return dict(self._mounted)

    def namespace_of(self, tool_name: str) -> str:
        """Longest mounted namespace that prefixes ``tool_name``, or ``"unknown"``."""
        best = ""
        for ns in self.namespaces:
            if tool_name.startswith(ns + "_") and len(ns) > len(best):
                best = ns
        return best or "unknown"

    # -- mount / unmount ----------------------------------------------------------------------

    def _mount(self, cfg: UpstreamConfig, *, previous_generation: int = 0) -> MountedUpstream:
        previous = self._mounted.get(cfg.id)
        generation = max(previous.generation if previous else 0, previous_generation) + 1
        now = datetime.now(UTC)

        def degraded(reason: str, kind: str, message: str) -> MountedUpstream:
            UPSTREAM_ERRORS.labels(cfg.id, kind).inc()
            UPSTREAM_UP.labels(upstream=cfg.id).set(0)
            log.error("upstream not mounted", upstream=cfg.id, reason=reason, error=message)
            return MountedUpstream(
                config=cfg,
                health=UpstreamHealth(
                    upstream=cfg.id,
                    status="degraded",
                    mounted=False,
                    checked_at=now,
                    reason=reason,
                    error=message,
                ),
                generation=generation,
            )

        if cfg.transport == "stdio" and self._is_prod:
            return degraded(
                "stdio_forbidden_in_prod",
                "stdio_forbidden",
                "stdio upstreams are not allowed when GATEWAY_ENV=prod; wrap the server in "
                "an HTTP shim (PLAN section 3.4)",
            )

        secret: str | None = None
        if cfg.auth.secret_ref:
            try:
                secret = resolve_secret(cfg.auth.secret_ref, self._secrets_dir)
            except SecretMissing as exc:
                return degraded("secret_missing", "secret_missing", str(exc))

        try:
            built = build_proxy(cfg, secret=secret, mask_error_details=self._is_prod)
        except UnsupportedUpstream as exc:
            return degraded(exc.reason, exc.reason, str(exc))
        except Exception as exc:
            return degraded("build_failed", "build_failed", f"{type(exc).__name__}: {exc}")

        before = list(self._mcp.providers)
        try:
            self._mcp.mount(built.proxy, namespace=cfg.id)
        except Exception as exc:
            return degraded("mount_failed", "mount_failed", f"{type(exc).__name__}: {exc}")
        added = [p for p in self._mcp.providers if not any(p is q for q in before)]
        if len(added) != 1:
            # ADR 0005: live unmount relies on mount() appending exactly one entry to the
            # public ``providers`` list. If FastMCP changes that, fail loudly here (and in
            # tests) instead of leaking providers on every config change.
            raise RuntimeError(
                f"FastMCP.mount appended {len(added)} providers (expected 1); live unmount "
                "per ADR 0005 is broken on this FastMCP version"
            )
        UPSTREAM_UP.labels(upstream=cfg.id).set(0)  # unknown until the first probe
        log.info(
            "upstream mounted",
            upstream=cfg.id,
            transport=cfg.transport,
            url=cfg.url,
            command=cfg.command,
            session_mode=built.session_mode,
            protocol_mode=cfg.protocol_mode,
            auth=cfg.auth.type,
            cache_ttl_sec=cfg.cache_ttl_sec,
        )
        return MountedUpstream(
            config=cfg,
            health=UpstreamHealth(
                upstream=cfg.id,
                status="unknown",
                mounted=True,
                session_mode=built.session_mode,
            ),
            built=built,
            provider=added[0],
            mounted_at=now,
            generation=generation,
        )

    async def _unmount(self, upstream_id: str, *, reason: str) -> None:
        entry = self._mounted.pop(upstream_id, None)
        if entry is None:
            return
        if entry.provider is not None:
            try:
                self._mcp.providers.remove(entry.provider)  # ADR 0005
            except ValueError:
                log.warning("provider already gone from FastMCP", upstream=upstream_id)
        if entry.built is not None and entry.built.shared is not None:
            await entry.built.shared.close()
        with contextlib.suppress(KeyError):
            UPSTREAM_UP.remove(upstream_id)
        log.info("upstream unmounted", upstream=upstream_id, reason=reason)

    async def unmount(self, upstream_id: str) -> None:
        async with self._lock:
            await self._unmount(upstream_id, reason="requested")
            self._update_mounted_gauge()

    def _update_mounted_gauge(self) -> None:
        UPSTREAMS_MOUNTED.set(len(self.namespaces))

    # -- health probe -----------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background probe loop (first probe runs immediately)."""
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop(), name="upstream-probe")

    async def stop(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._probe_task
            self._probe_task = None
        async with self._lock:
            for entry in self._mounted.values():
                if entry.built is not None and entry.built.shared is not None:
                    await entry.built.shared.close()

    @property
    def probe_runs(self) -> int:
        return self._probe_runs

    async def _probe_loop(self) -> None:
        while True:
            try:
                await self.probe_once()
            except Exception as exc:
                log.error("upstream probe round failed", error=str(exc))
            await asyncio.sleep(self._probe_interval)

    async def probe_once(self) -> Mapping[str, UpstreamHealth]:
        entries = [e for e in self._mounted.values() if e.is_mounted]
        await asyncio.gather(*(self._probe(e) for e in entries))
        self._probe_runs += 1
        return self.health()

    async def _probe(self, entry: MountedUpstream) -> None:
        assert entry.built is not None
        upstream_id = entry.config.id
        start = time.perf_counter()
        error: str | None = None
        tool_count: int | None = None
        try:
            with anyio.fail_after(self._probe_timeout):
                client = entry.built.base_client.new()
                async with client:
                    tool_count = len(await client.list_tools())
        except TimeoutError:
            error = f"probe timed out after {self._probe_timeout}s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
        latency = time.perf_counter() - start
        UPSTREAM_PROBE_SECONDS.labels(upstream_id).observe(latency)

        current = self._mounted.get(upstream_id)
        if current is not entry or entry.generation != current.generation:
            return  # unmounted or re-mounted while we were probing
        up = error is None
        previous = entry.health.status
        entry.health = UpstreamHealth(
            upstream=upstream_id,
            status="up" if up else "down",
            mounted=True,
            checked_at=datetime.now(UTC),
            error=error,
            tool_count=tool_count,
            latency_ms=round(latency * 1000, 1),
            session_mode=entry.built.session_mode,
        )
        UPSTREAM_UP.labels(upstream=upstream_id).set(1 if up else 0)
        if not up:
            UPSTREAM_ERRORS.labels(upstream_id, "probe_failed").inc()
        if previous != entry.health.status:
            log.log(
                20 if up else 40,  # INFO on recovery, ERROR on failure
                "upstream health changed",
                upstream=upstream_id,
                status=entry.health.status,
                error=error,
                tool_count=tool_count,
                latency_ms=entry.health.latency_ms,
            )
