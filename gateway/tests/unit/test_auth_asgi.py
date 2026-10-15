"""HTTP-level tests of the bearer middleware around /mcp (TestClient, no network)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from prometheus_client import REGISTRY
from starlette.testclient import TestClient

from mcp_gateway.app import build_gateway
from mcp_gateway.auth.jwt import JwtAuth
from mcp_gateway.settings import Settings, load_settings
from tests.jwt_support import FakeIdp

ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
BASE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}
WHOAMI = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "gateway_whoami", "arguments": {}, "_meta": ENVELOPE},
}
WHOAMI_HEADERS = {**BASE_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "gateway_whoami"}


def auth_count(result: str) -> float:
    return REGISTRY.get_sample_value("gateway_auth_results_total", {"result": result}) or 0.0


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def make_client(idp: FakeIdp, **overrides: Any) -> Iterator[TestClient]:
    settings: Settings = load_settings(
        metrics_port=0,
        log_json=False,
        log_level="INFO",
        static_upstreams=[],
        oidc_issuer=idp.issuer,
        policy_source="static",
        **overrides,
    )
    gw = build_gateway(settings, jwt_auth=JwtAuth(settings, http_client=idp.client()))
    with TestClient(gw.app, base_url="http://localhost") as c:
        yield c


@pytest.fixture
def client(idp: FakeIdp) -> Iterator[TestClient]:
    yield from make_client(idp)


@pytest.fixture
def strict_client(idp: FakeIdp) -> Iterator[TestClient]:
    yield from make_client(idp, allow_anonymous=False)


def whoami(client: TestClient, **headers: str) -> dict[str, Any]:
    r = client.post("/mcp", json=WHOAMI, headers={**WHOAMI_HEADERS, **headers})
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"]


def assert_401(r: Any, *, error: str | None = "invalid_token") -> None:
    assert r.status_code == 401, r.text
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith(
        'Bearer resource_metadata="http://localhost:8080/.well-known/oauth-protected-resource"'
    )
    if error:
        assert f'error="{error}"' in challenge
        assert r.json()["error"] == error
    else:
        assert "error=" not in challenge
    assert r.headers["cache-control"] == "no-store"
    assert "jsonrpc" not in r.json()


def test_no_header_is_anonymous(client: TestClient) -> None:
    before = auth_count("anonymous")
    p = whoami(client)
    assert p["kind"] == "anonymous" and p["sub"] is None
    assert auth_count("anonymous") == before + 1


def test_valid_token_is_user(
    idp: FakeIdp, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_gateway.audit")
    before = auth_count("valid")
    p = whoami(client, Authorization=f"Bearer {idp.token()}")
    assert p["kind"] == "user"
    assert p["sub"] == "user-1"
    assert p["client_id"] == "mcp-gateway"
    assert p["roles"] == ["mcp-developer"]
    assert p["scopes"] == ["openid", "profile"]
    assert auth_count("valid") == before + 1
    audit = [
        r for r in caplog.records if r.name == "mcp_gateway.audit" and "tools.call" in str(r.msg)
    ]
    assert audit, "audit middleware should log the call"
    event = audit[-1].msg  # structlog event dict
    assert isinstance(event, dict)
    assert event["principal_kind"] == "user"
    assert event["sub"] == "user-1"
    assert event["client_id"] == "mcp-gateway"
    assert event["tool"] == "gateway_whoami"


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(lambda idp: idp.token(exp=0), id="expired"),
        pytest.param(lambda idp: idp.token(nbf=2**31), id="nbf-future"),
        pytest.param(lambda idp: idp.token(aud="other-api"), id="wrong-aud"),
        pytest.param(lambda idp: idp.token(iss="https://evil.test"), id="wrong-iss"),
        pytest.param(lambda idp: idp.token(alg="none"), id="alg-none"),
        pytest.param(lambda idp: idp.hs256_with_public_key(), id="hs256-pubkey"),
        pytest.param(lambda idp: idp.token(header={"kid": "ghost"}), id="unknown-kid"),
        pytest.param(lambda idp: "garbage", id="garbage"),
        pytest.param(lambda idp: "", id="empty-bearer"),
    ],
)
def test_invalid_tokens_are_401(idp: FakeIdp, client: TestClient, make_token: Any) -> None:
    before = auth_count("invalid")
    r = client.post(
        "/mcp",
        json=WHOAMI,
        headers={**WHOAMI_HEADERS, "Authorization": f"Bearer {make_token(idp)}"},
    )
    assert_401(r)
    assert auth_count("invalid") == before + 1


def test_unknown_kid_then_refresh_over_http(idp: FakeIdp) -> None:
    for c in make_client(idp, jwks_refresh_floor_sec=0):
        assert whoami(c, Authorization=f"Bearer {idp.token('k1')}")["kind"] == "user"
        idp.add_key("k2")
        assert whoami(c, Authorization=f"Bearer {idp.token('k2')}")["sub"] == "user-1"
        assert idp.jwks_fetches == 2


def test_token_in_query_string_or_cookie_is_ignored(idp: FakeIdp, client: TestClient) -> None:
    tok = idp.token()
    client.cookies.set("access_token", tok)
    try:
        r = client.post(f"/mcp?access_token={tok}", json=WHOAMI, headers=WHOAMI_HEADERS)
    finally:
        client.cookies.clear()
    assert r.status_code == 200
    assert r.json()["result"]["structuredContent"]["kind"] == "anonymous"


def test_basic_scheme_is_401(client: TestClient) -> None:
    r = client.post(
        "/mcp", json=WHOAMI, headers={**WHOAMI_HEADERS, "Authorization": "Basic ZGV2MTpkZXYx"}
    )
    assert_401(r)
    r = client.post("/mcp", json=WHOAMI, headers={**WHOAMI_HEADERS, "Authorization": "Bearer"})
    assert_401(r)


def test_allow_anonymous_false(idp: FakeIdp, strict_client: TestClient) -> None:
    before = auth_count("disabled_anonymous")
    r = strict_client.post("/mcp", json=WHOAMI, headers=WHOAMI_HEADERS)
    assert_401(r, error=None)
    assert r.json()["error"] == "unauthorized"
    assert auth_count("disabled_anonymous") == before + 1
    assert whoami(strict_client, Authorization=f"Bearer {idp.token()}")["kind"] == "user"
    r = strict_client.post(
        "/mcp", json=WHOAMI, headers={**WHOAMI_HEADERS, "Authorization": "Bearer x"}
    )
    assert_401(r)


def test_only_mcp_is_guarded(strict_client: TestClient) -> None:
    for path in ("/healthz", "/readyz", "/metrics", "/.well-known/oauth-protected-resource"):
        r = strict_client.get(path)
        assert r.status_code == 200, path
        assert "www-authenticate" not in r.headers


def test_protected_resource_metadata(idp: FakeIdp) -> None:
    for c in make_client(idp, scopes_supported=["openid", "mcp:tools"]):
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            r = c.get(path)
            assert r.status_code == 200
            assert r.json() == {
                "resource": "http://localhost:8080/mcp",
                "authorization_servers": [idp.issuer],
                "scopes_supported": ["openid", "mcp:tools"],
                "bearer_methods_supported": ["header"],
                "resource_name": "MCP gateway",
            }
            assert r.headers["access-control-allow-origin"] == "*"
            assert c.options(path).status_code == 204


def test_readyz_reports_oidc(client: TestClient, idp: FakeIdp) -> None:
    checks = client.get("/readyz").json()["checks"]
    assert checks["oidc"] == {
        "ok": True,
        "critical": False,
        "issuer": idp.issuer,
        "jwks_uri": idp.jwks_uri,
        "configured": True,
    }
