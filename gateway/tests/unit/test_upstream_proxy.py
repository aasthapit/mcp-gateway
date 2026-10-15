from __future__ import annotations

import httpx2
import pytest
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyProvider

from mcp_gateway.upstreams.models import UpstreamConfig
from mcp_gateway.upstreams.proxy import (
    SharedClient,
    UnsupportedUpstream,
    build_proxy,
    effective_session_mode,
    make_httpx_factory,
    static_headers,
)


def cfg(**over: object) -> UpstreamConfig:
    base: dict = {
        "_id": "up",
        "displayName": "Up",
        "transport": "http",
        "url": "http://127.0.0.1:1/mcp",
        "auth": {"type": "none"},
        "sessionMode": "per_request",
        "timeoutsMs": {"connect": 1500, "call": 45000},
        "cacheTtlSec": 7,
    }
    base.update(over)
    return UpstreamConfig.model_validate(base)


def test_static_headers() -> None:
    assert static_headers(cfg(), None) == {}
    assert static_headers(cfg(auth={"type": "static_bearer", "secretRef": "r"}), "s3") == {
        "Authorization": "Bearer s3"
    }
    assert static_headers(
        cfg(auth={"type": "static_header", "secretRef": "r", "headerName": "X-Api-Key"}), "k"
    ) == {"X-Api-Key": "k"}
    with pytest.raises(UnsupportedUpstream) as exc:
        static_headers(cfg(auth={"type": "static_bearer", "secretRef": "r"}), None)
    assert exc.value.reason == "secret_missing"
    with pytest.raises(UnsupportedUpstream) as exc:
        static_headers(cfg(auth={"type": "client_credentials"}), None)
    assert exc.value.reason == "auth_unsupported"


def test_effective_session_mode_rules() -> None:
    assert effective_session_mode(cfg()) == ("per_request", None)
    assert effective_session_mode(cfg(sessionMode="shared", protocolMode="2026-07-28")) == (
        "shared",
        None,
    )
    mode, reason = effective_session_mode(cfg(sessionMode="shared"))
    assert mode == "per_request" and "protocolMode=2026-07-28" in (reason or "")
    mode, reason = effective_session_mode(
        cfg(sessionMode="shared", transport="sse", protocolMode="2026-07-28")
    )
    assert mode == "per_request" and "transport=http" in (reason or "")
    mode, reason = effective_session_mode(
        cfg(
            sessionMode="shared",
            transport="stdio",
            url=None,
            command="x",
            protocolMode="2026-07-28",
        )
    )
    assert mode == "per_request"


def test_httpx_factory_applies_connect_and_call_timeouts() -> None:
    c = cfg()
    client = make_httpx_factory(c.timeouts_ms)(headers={"A": "b"}, follow_redirects=True)
    assert isinstance(client, httpx2.AsyncClient)
    assert client.timeout.connect == 1.5 and client.timeout.pool == 1.5
    assert client.timeout.read == 45.0 and client.timeout.write == 45.0
    assert client.headers["A"] == "b"
    sse = make_httpx_factory(c.timeouts_ms, stream_read_timeout=False)()
    assert sse.timeout.read is None and sse.timeout.connect == 1.5


def test_build_proxy_per_request() -> None:
    built = build_proxy(cfg(auth={"type": "static_bearer", "secretRef": "r"}), secret="tok")
    assert built.session_mode == "per_request" and built.shared is None
    assert isinstance(built.proxy, FastMCPProxy) and built.proxy.name == "up"
    client = built.base_client
    assert isinstance(client, ProxyClient)
    assert client._transport_options is not None
    assert client._transport_options.forward_incoming_headers is False  # ADR 0001
    assert client.mode == "auto"
    assert client.transport.headers == {"Authorization": "Bearer tok"}
    assert client.transport.httpx_client_factory is not None
    assert client._session_kwargs["read_timeout_seconds"] == 45.0
    assert client._init_timeout == 1.5
    providers = [p for p in built.proxy.providers if isinstance(p, ProxyProvider)]
    assert len(providers) == 1 and providers[0]._cache_ttl == 7.0
    # per_request: the factory hands out a fresh disconnected clone every time
    a, b = built.proxy.client_factory(), built.proxy.client_factory()
    assert a is not b and not a.is_connected()


def test_build_proxy_shared_uses_shared_client_factory() -> None:
    built = build_proxy(cfg(sessionMode="shared", protocolMode="2026-07-28"))
    assert built.session_mode == "shared" and isinstance(built.shared, SharedClient)
    assert built.proxy.client_factory == built.shared.get
    assert built.base_client.mode == "2026-07-28"


def test_build_proxy_stdio_and_sse() -> None:
    sse = build_proxy(cfg(transport="sse"))
    assert type(sse.base_client.transport).__name__ == "SSETransport"
    stdio = build_proxy(cfg(transport="stdio", url=None, command="python", args=["-m", "x"]))
    assert type(stdio.base_client.transport).__name__ == "StdioTransport"
    assert stdio.base_client.transport.command == "python"
