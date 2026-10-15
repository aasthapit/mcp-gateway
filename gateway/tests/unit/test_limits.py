"""WP7 limits: per-principal rate limiting, response truncation, max body size, client IP."""

from __future__ import annotations

import pytest
from fastmcp import Client
from prometheus_client import REGISTRY
from starlette.testclient import TestClient

from mcp_gateway.app import build_gateway
from mcp_gateway.auth.context import bind_principal
from mcp_gateway.auth.principal import ANONYMOUS, Principal
from mcp_gateway.http_guards import client_ip_from_scope, parse_networks, route_group
from mcp_gateway.middleware.limits import PrincipalRateLimitingMiddleware, rate_limit_key
from mcp_gateway.policy.snapshot import AllowAllSnapshot
from mcp_gateway.settings import Settings


def counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def scope(peer: str | None, xff: str | None = None) -> dict:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return {"type": "http", "client": (peer, 1234) if peer else None, "headers": headers}


# -- client IP -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peer", "xff", "trusted", "expected"),
    [
        ("203.0.113.9", None, [], "203.0.113.9"),
        ("203.0.113.9", "1.2.3.4", [], "203.0.113.9"),  # untrusted peer: XFF ignored
        ("10.128.2.7", "1.2.3.4", ["10.128.0.0/14"], "1.2.3.4"),
        # client-supplied left entries are skipped; right-most untrusted wins
        ("10.128.2.7", "6.6.6.6, 1.2.3.4", ["10.128.0.0/14"], "1.2.3.4"),
        # chain of trusted proxies
        ("10.128.2.7", "1.2.3.4, 10.129.0.5", ["10.128.0.0/14"], "1.2.3.4"),
        # all trusted: the left-most one
        ("10.128.2.7", "10.130.0.1", ["10.128.0.0/14"], "10.130.0.1"),
        # garbage stops the walk at the last good hop
        ("10.128.2.7", "nonsense, 10.129.0.5", ["10.128.0.0/14"], "10.129.0.5"),
        ("10.128.2.7", None, ["10.128.0.0/14"], "10.128.2.7"),
        ("10.128.2.7", "[2001:db8::1]:443", ["10.128.0.0/14"], "2001:db8::1"),
        ("10.128.2.7", "198.51.100.4:5555", ["10.128.0.0/14"], "198.51.100.4"),
        (None, "1.2.3.4", ["0.0.0.0/0"], "unknown"),
    ],
)
def test_client_ip(peer: str | None, xff: str | None, trusted: list[str], expected: str) -> None:
    assert client_ip_from_scope(scope(peer, xff), parse_networks(trusted)) == expected


def test_route_group() -> None:
    assert route_group("/internal/catalog") == "internal"
    assert route_group("/readyz") == "probe"
    assert route_group("/mcp") == "mcp"
    assert route_group("/favicon.ico") == "other"


def test_settings_reject_bad_proxy_cidr() -> None:
    with pytest.raises(ValueError):
        Settings(trusted_proxies="10.0.0.0/33")


# -- rate limiting -------------------------------------------------------------------------


def test_rate_limit_keys() -> None:
    assert rate_limit_key(ANONYMOUS, lambda: "1.2.3.4") == "ip:1.2.3.4"
    assert rate_limit_key(Principal("user", sub="u1", client_id="c"), lambda: "x") == "user:u1"
    assert rate_limit_key(Principal("user", client_id="ci-runner"), lambda: "x") == "user:ci-runner"
    assert rate_limit_key(Principal("user"), lambda: "9.9.9.9") == "ip:9.9.9.9"


def test_bucket_table_is_bounded_and_shaped_per_kind() -> None:
    mw = PrincipalRateLimitingMiddleware(
        anonymous_rps=1, anonymous_burst=2, user_rps=5, user_burst=7, max_keys=100
    )
    assert mw.limiters["user:a"].capacity == 7
    assert mw.limiters["ip:1.1.1.1"].capacity == 2
    for i in range(250):
        mw.limiters[f"ip:10.0.0.{i}"]
    assert len(mw.limiters) == 100
    assert "user:a" not in mw.limiters  # least recently used went first


def _limited_settings(settings: Settings, **kw: object) -> Settings:
    base: dict[str, object] = {
        "static_upstreams": [],
        "ready_requires_upstream": False,
        "rate_limit_anonymous_rps": 0.001,
        "rate_limit_anonymous_burst": 3,
        "rate_limit_user_rps": 0.001,
        "rate_limit_user_burst": 5,
    }
    base.update(kw)
    return settings.model_copy(update=base)


async def test_rate_limit_rejects_after_burst_per_principal(settings: Settings) -> None:
    gw = build_gateway(_limited_settings(settings))
    gw.set_snapshot(AllowAllSnapshot())  # no lifespan with the in-memory client

    @gw.mcp.tool(name="ping_tool")
    def ping_tool() -> str:
        return "pong"

    async def calls_until_limited(c: Client) -> int:
        """Successful tools/call before the first rejection (every MCP request, including
        the client's own discover/list, takes a token)."""
        for ok in range(20):
            try:
                assert (await c.call_tool("ping_tool", {})).data == "pong"
            except Exception as exc:
                assert "Rate limit exceeded" in str(exc)
                return ok
        raise AssertionError("never rate limited")

    anon0 = counter("gateway_rate_limited_total", principal_kind="anonymous")
    user0 = counter("gateway_rate_limited_total", principal_kind="user")
    async with Client(gw.mcp) as c:  # in-memory transport: anonymous, client IP "unknown"
        assert 1 <= await calls_until_limited(c) < 3  # burst 3 minus discover/list
        assert counter("gateway_rate_limited_total", principal_kind="anonymous") == anon0 + 1

        with bind_principal(Principal("user", sub="alice")):  # a bucket of its own
            assert 3 <= await calls_until_limited(c) <= 5
            with pytest.raises(Exception, match="Rate limit exceeded"):
                await c.list_tools()
        assert counter("gateway_rate_limited_total", principal_kind="user") == user0 + 2
        with bind_principal(Principal("user", sub="bob")):
            assert (await c.call_tool("ping_tool", {})).data == "pong"


async def test_rate_limit_can_be_disabled(settings: Settings) -> None:
    gw = build_gateway(_limited_settings(settings, rate_limit_enabled=False))
    assert "PrincipalRateLimitingMiddleware" not in {type(m).__name__ for m in gw.mcp.middleware}


def test_anonymous_bucket_is_per_client_ip_over_http(settings: Settings) -> None:
    s = _limited_settings(settings, trusted_proxies=["127.0.0.1"])
    gw = build_gateway(s)

    @gw.mcp.tool(name="ping_tool")
    def ping_tool() -> str:
        return "pong"

    def call(c: TestClient, ip: str) -> dict:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ping_tool",
                    "arguments": {},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "ping_tool",
                "X-Forwarded-For": ip,
            },
        )
        assert r.status_code == 200, r.text
        return r.json()

    # The TestClient peer is a trusted loopback proxy, so X-Forwarded-For names the client.
    with TestClient(gw.app, client=("127.0.0.1", 50000)) as c:
        for _ in range(3):
            assert "result" in call(c, "198.51.100.1")
        assert call(c, "198.51.100.1")["error"]["message"].startswith("Rate limit exceeded")
        assert "result" in call(c, "198.51.100.2")  # another client IP, its own bucket


# -- response limiting ---------------------------------------------------------------------


async def test_oversized_tool_result_is_truncated_and_counted(settings: Settings) -> None:
    gw = build_gateway(
        _limited_settings(settings, rate_limit_enabled=False, response_max_bytes=2_000)
    )

    @gw.mcp.tool(name="big_tool")
    def big_tool() -> str:
        return "x" * 50_000

    @gw.mcp.tool(name="small_tool")
    def small_tool() -> str:
        return "tiny"

    gw.set_snapshot(AllowAllSnapshot())
    before = counter("gateway_response_truncated_total", upstream="unknown")
    async with Client(gw.mcp) as c:
        res = await c.call_tool("big_tool", {})
        text = res.content[0].text
        assert len(text.encode()) <= 2_000
        assert text.endswith("[Response truncated due to size limit]")
        assert (await c.call_tool("small_tool", {})).data == "tiny"
    assert counter("gateway_response_truncated_total", upstream="unknown") == before + 1


# -- max body size -------------------------------------------------------------------------


def test_body_over_limit_gets_413(settings: Settings) -> None:
    gw = build_gateway(_limited_settings(settings, max_request_body_bytes=1_000))
    before = counter("gateway_request_body_rejected_total")
    with TestClient(gw.app) as c:
        r = c.post(
            "/mcp", content=b"{" + b" " * 2_000 + b"}", headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 413
        assert r.json()["error"] == "request_too_large"

        def chunks():
            for _ in range(5):
                yield b" " * 400

        r = c.post("/mcp", content=chunks(), headers={"Content-Type": "application/json"})
        assert r.status_code == 413  # no Content-Length: buffered up to the limit, then 413

        # under the limit (chunked): reaches MCP, which answers the bad JSON itself
        r = c.post("/mcp", content=iter([b"{}"]), headers={"Content-Type": "application/json"})
        assert r.status_code != 413
        assert c.get("/healthz").status_code == 200
    assert counter("gateway_request_body_rejected_total") == before + 2


def test_http_responses_are_counted_by_route_and_class(settings: Settings) -> None:
    gw = build_gateway(_limited_settings(settings))
    ok0 = counter("gateway_http_responses_total", route="probe", code="2xx")
    nf0 = counter("gateway_http_responses_total", route="internal", code="4xx")
    with TestClient(gw.app) as c:
        c.get("/healthz")
        c.get("/internal/catalog")  # 404: no internal token configured
    assert counter("gateway_http_responses_total", route="probe", code="2xx") == ok0 + 1
    assert counter("gateway_http_responses_total", route="internal", code="4xx") == nf0 + 1
