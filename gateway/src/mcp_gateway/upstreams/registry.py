"""Mounts upstream MCP servers on the gateway as namespaced FastMCP proxies.

Interface fixed by docs/PLAN.md section 5. WP1 mounts a static list from settings;
WP4 drives :meth:`UpstreamRegistry.apply` from the policy snapshot, adds secret-backed
credentials, session modes, timeouts and health probes.

Proxy wiring (see docs/adr/0001-fastmcp-proxy-wiring.md): each upstream becomes
``create_proxy(ProxyClient(transport, forward_incoming_headers=False))`` mounted with
``mcp.mount(proxy, namespace=<id>)``. ``forward_incoming_headers=False`` is what keeps the
caller's ``Authorization`` header from leaking upstream.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyProvider

from mcp_gateway.telemetry import get_logger
from mcp_gateway.upstreams.models import CatalogTool, UpstreamConfig, UpstreamHealth

if TYPE_CHECKING:
    from mcp_gateway.policy.snapshot import PolicySnapshot
    from mcp_gateway.settings import Settings, StaticUpstream

log = get_logger(__name__)


def static_to_config(static: StaticUpstream) -> UpstreamConfig:
    return UpstreamConfig(
        _id=static.id,
        displayName=static.id,
        enabled=True,
        transport=static.transport,
        url=static.url,
        cacheTtlSec=static.cache_ttl_sec,
        sessionMode="per_request",
        protocol_mode=static.protocol_mode,
    )


def build_proxy(cfg: UpstreamConfig, *, headers: Mapping[str, str] | None = None) -> FastMCPProxy:
    """Create the FastMCP proxy server for one upstream (not yet mounted)."""
    if cfg.transport == "stdio":
        raise ValueError(f"upstream {cfg.id!r}: stdio transport is not supported by WP1")
    if not cfg.url:
        raise ValueError(f"upstream {cfg.id!r}: url is required for transport {cfg.transport}")

    transport: Any
    if cfg.transport == "sse":
        transport = SSETransport(cfg.url, headers=dict(headers or {}))
    else:
        transport = StreamableHttpTransport(cfg.url, headers=dict(headers or {}))

    client = ProxyClient(
        transport,
        name=f"gateway-upstream-{cfg.id}",
        forward_incoming_headers=False,
        mode=cfg.protocol_mode,
    )
    proxy = create_proxy(client, name=cfg.id, provider_error_strategy="warn")
    # create_proxy builds ProxyProvider(client_factory) with the 300 s default TTL;
    # the upstream config owns that knob.
    for provider in proxy.providers:
        if isinstance(provider, ProxyProvider):
            provider._cache_ttl = float(cfg.cache_ttl_sec)
    return proxy


class UpstreamRegistry:
    """Owns the set of mounted upstreams on a FastMCP server."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        self._configs: dict[str, UpstreamConfig] = {}
        self._proxies: dict[str, FastMCPProxy] = {}
        self._mounted_at: dict[str, datetime] = {}

    # -- WP1 ------------------------------------------------------------------------

    def mount_static(self, settings: Settings) -> None:
        for static in settings.static_upstreams:
            self.mount(static_to_config(static))

    def mount(self, cfg: UpstreamConfig) -> None:
        if cfg.id in self._proxies:
            log.debug("upstream already mounted", upstream=cfg.id)
            return
        proxy = build_proxy(cfg)
        self._mcp.mount(proxy, namespace=cfg.id)
        self._configs[cfg.id] = cfg
        self._proxies[cfg.id] = proxy
        self._mounted_at[cfg.id] = datetime.now(UTC)
        log.info("upstream mounted", upstream=cfg.id, url=cfg.url, transport=cfg.transport)

    # -- Section 5 interface ------------------------------------------------------------

    async def apply(self, snapshot: PolicySnapshot) -> None:
        """Mount upstreams that are enabled in ``snapshot`` and not yet mounted.

        Removal is deliberately not done here: disabled upstreams are hidden by policy
        at once and physically unmounted on the next rolling restart (PLAN section 7,
        decision 10; ADR 0005 records that an in-place removal is possible if WP4 wants it).
        """
        for cfg in snapshot.upstreams.values():
            if cfg.enabled and cfg.id not in self._proxies:
                try:
                    self.mount(cfg)
                except Exception as exc:  # one bad upstream must not block the rest
                    log.error("upstream mount failed", upstream=cfg.id, error=str(exc))

    async def catalog(self) -> list[CatalogTool]:
        """All upstream tools, namespaced, for ``/internal/catalog``."""
        out: list[CatalogTool] = []
        for upstream_id, proxy in self._proxies.items():
            try:
                tools = await proxy.list_tools()
            except Exception as exc:
                log.warning("catalog: upstream unavailable", upstream=upstream_id, error=str(exc))
                continue
            out.extend(
                CatalogTool(
                    name=f"{upstream_id}_{t.name}",
                    upstream=upstream_id,
                    original_name=t.name,
                    description=t.description,
                )
                for t in tools
            )
        return out

    def health(self) -> Mapping[str, UpstreamHealth]:
        """WP1: mounted means up. WP4 replaces this with background probes."""
        return {
            uid: UpstreamHealth(upstream=uid, up=True, checked_at=self._mounted_at[uid])
            for uid in self._proxies
        }

    # -- helpers used by middleware ---------------------------------------------------------

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(self._proxies)

    @property
    def configs(self) -> Mapping[str, UpstreamConfig]:
        return dict(self._configs)

    def namespace_of(self, tool_name: str) -> str:
        """Longest mounted namespace that prefixes ``tool_name``, or ``"unknown"``."""
        best = ""
        for ns in self._proxies:
            if tool_name.startswith(ns + "_") and len(ns) > len(best):
                best = ns
        return best or "unknown"
