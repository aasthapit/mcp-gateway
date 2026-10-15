"""Build the FastMCP proxy for one upstream (docs/adr/0001, docs/adr/0009).

Wiring, per ADR 0001::

    transport = StreamableHttpTransport(url, headers=static_auth, httpx_client_factory=...)
    client    = ProxyClient(transport, forward_incoming_headers=False, mode=protocol_mode,
                            timeout=call_s, init_timeout=connect_s)
    proxy     = create_proxy(client, name=id, provider_error_strategy="warn")   # per_request
    proxy     = FastMCPProxy(client_factory=shared.get, name=id, ...)            # shared

Timeouts (ADR 0009): ``Client(timeout=)`` only sets the session read deadline, so the
connect deadline goes through the transport's ``httpx_client_factory``. ``sessionMode:
shared`` keeps one connected client per upstream per process; it is only allowed for
streamable HTTP upstreams pinned to the ``2026-07-28`` era, where every request is a
self-contained POST and nothing of one caller can leak into another's session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
from fastmcp.client import Client
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyProvider

from mcp_gateway.telemetry import get_logger
from mcp_gateway.upstreams.models import SessionMode, UpstreamConfig, UpstreamTimeouts

log = get_logger(__name__)


class UnsupportedUpstream(ValueError):
    """The config is valid but this gateway version cannot mount it (``reason`` is a
    low-cardinality token used in metrics and health)."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# -- auth ------------------------------------------------------------------------------


def static_headers(cfg: UpstreamConfig, secret: str | None) -> dict[str, str]:
    """Headers the gateway sends upstream on every request (the gateway's own credential)."""
    auth = cfg.auth
    if auth.type == "none":
        return {}
    if auth.type == "client_credentials":
        raise UnsupportedUpstream(
            "auth_unsupported", f"upstream {cfg.id!r}: auth.type=client_credentials is v2"
        )
    if secret is None:
        raise UnsupportedUpstream("secret_missing", f"upstream {cfg.id!r}: secret not resolved")
    if auth.type == "static_bearer":
        return {"Authorization": f"Bearer {secret}"}
    assert auth.header_name  # validated by the model
    return {auth.header_name: secret}


# -- timeouts ---------------------------------------------------------------------------


def make_httpx_factory(timeouts: UpstreamTimeouts, *, stream_read_timeout: bool = True) -> Any:
    """``httpx_client_factory`` for the HTTP/SSE transports (ADR 0009).

    The transport calls ``factory(headers=, auth=, follow_redirects=, timeout=?)``; the
    ``timeout`` it may pass only carries the session read deadline, so connect/pool come
    from our config. For SSE the long-lived event stream must not hit a read timeout, so
    ``stream_read_timeout=False`` leaves ``read`` unbounded and the per-request deadline
    is enforced by the MCP session (``Client(timeout=)``) instead.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,
    ) -> httpx2.AsyncClient:
        client_kwargs: dict[str, Any] = {
            "timeout": httpx2.Timeout(
                connect=timeouts.connect_s,
                read=timeouts.call_s if stream_read_timeout else None,
                write=timeouts.call_s,
                pool=timeouts.connect_s,
            )
        }
        if headers is not None:
            client_kwargs["headers"] = headers
        if auth is not None:
            client_kwargs["auth"] = auth
        if "follow_redirects" in kwargs:
            client_kwargs["follow_redirects"] = kwargs["follow_redirects"]
        return httpx2.AsyncClient(**client_kwargs)

    return factory


# -- shared session ---------------------------------------------------------------------


class SharedClient:
    """One connected client reused by every request to a stateless modern-era upstream.

    FastMCP's proxy components do ``async with client:`` around each backend call; on a
    connected client that only bumps a nesting counter, so the connection (and its httpx
    pool) survives between calls. If the backend session dies (upstream restarted, network
    blip) ``is_connected()`` turns false and :meth:`get` reconnects before handing the
    client out. ``create_proxy`` refuses to reuse a connected ``ProxyClient`` (it clones a
    fresh session per request), which is why the registry builds ``FastMCPProxy`` with this
    object's :meth:`get` as the ``client_factory`` instead.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._lock = asyncio.Lock()
        self.reconnects = 0

    @property
    def client(self) -> Client:
        return self._client

    def is_connected(self) -> bool:
        return self._client.is_connected()

    async def get(self) -> Client:
        async with self._lock:
            if not self._client.is_connected():
                # Reset whatever a dead session left behind (nesting counter, finished
                # task) before starting a new one; a failed stop must not block us.
                try:
                    await self._client.close()
                except Exception as exc:
                    log.debug("shared client cleanup failed", error=str(exc))
                await self._client.__aenter__()
                self.reconnects += 1
            return self._client

    async def close(self) -> None:
        async with self._lock:
            try:
                await self._client.close()
            except Exception as exc:
                log.debug("shared client close failed", error=str(exc))


# -- proxy ------------------------------------------------------------------------------


@dataclass
class BuiltProxy:
    proxy: FastMCPProxy
    base_client: ProxyClient
    """Disconnected template; ``base_client.new()`` gives an independent client for probes."""
    session_mode: SessionMode
    """Effective mode after the rules below."""
    session_mode_reason: str | None
    """Why the configured mode was overridden, if it was."""
    shared: SharedClient | None


def effective_session_mode(cfg: UpstreamConfig) -> tuple[SessionMode, str | None]:
    if cfg.session_mode != "shared":
        return "per_request", None
    if cfg.transport != "http":
        return (
            "per_request",
            f"sessionMode=shared needs transport=http (got {cfg.transport}; "
            "sse is handshake-only and stdio owns a process)",
        )
    if cfg.protocol_mode != "2026-07-28":
        return (
            "per_request",
            f"sessionMode=shared needs protocolMode=2026-07-28 (got {cfg.protocol_mode}); "
            "a handshake-era session is stateful and must not be shared between callers",
        )
    return "shared", None


def build_transport(cfg: UpstreamConfig, headers: Mapping[str, str]) -> Any:
    if cfg.transport == "stdio":
        assert cfg.command
        return StdioTransport(cfg.command, list(cfg.args))
    assert cfg.url
    if cfg.transport == "sse":
        return SSETransport(
            cfg.url,
            headers=dict(headers),
            httpx_client_factory=make_httpx_factory(cfg.timeouts_ms, stream_read_timeout=False),
        )
    return StreamableHttpTransport(
        cfg.url,
        headers=dict(headers),
        httpx_client_factory=make_httpx_factory(cfg.timeouts_ms),
    )


def build_proxy(
    cfg: UpstreamConfig, *, secret: str | None = None, mask_error_details: bool = False
) -> BuiltProxy:
    """Create the (not yet mounted) FastMCP proxy server for one upstream.

    Raises :class:`UnsupportedUpstream` for configs this version cannot serve.
    """
    headers = static_headers(cfg, secret)
    transport = build_transport(cfg, headers)
    client = ProxyClient(
        transport,
        name=f"gateway-upstream-{cfg.id}",
        forward_incoming_headers=False,  # the caller's bearer never reaches the upstream
        mode=cfg.protocol_mode,
        timeout=cfg.timeouts_ms.call_s,  # per-request deadline (session read timeout)
        init_timeout=cfg.timeouts_ms.connect_s,  # protocol negotiation deadline
    )
    mode, reason = effective_session_mode(cfg)
    settings: dict[str, Any] = {
        "name": cfg.id,
        "provider_error_strategy": "warn",  # a dead upstream is skipped in aggregate lists
        "mask_error_details": mask_error_details,  # match the parent (mount() warns otherwise)
    }
    shared: SharedClient | None = None
    if mode == "shared":
        shared = SharedClient(client)
        proxy = FastMCPProxy(client_factory=shared.get, **settings)
    else:
        proxy = create_proxy(client, **settings)
    # create_proxy/FastMCPProxy build ProxyProvider(client_factory) with the 300 s default
    # TTL and expose no knob; the upstream config owns it (private attribute, ADR 0001).
    for provider in proxy.providers:
        if isinstance(provider, ProxyProvider):
            provider._cache_ttl = float(cfg.cache_ttl_sec)
    if reason:
        log.info("upstream session mode overridden", upstream=cfg.id, mode=mode, reason=reason)
    return BuiltProxy(
        proxy=proxy,
        base_client=client,
        session_mode=mode,
        session_mode_reason=reason,
        shared=shared,
    )
