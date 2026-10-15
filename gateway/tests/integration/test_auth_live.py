"""Auth against the local Keycloak (``make up``): realm ``mcp`` on localhost:8180.

Uses the module-scoped in-process gateway from ``test_gateway_live`` (default settings:
issuer http://localhost:8180/realms/mcp, discovery at startup).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client

from tests.conftest import port_open
from tests.integration.test_gateway_live import ENVELOPE, base_url, stack_available  # noqa: F401

pytestmark = pytest.mark.integration

KC = "http://localhost:8180/realms/mcp"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/list",
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": ENVELOPE}}


@pytest.fixture(scope="module")
def keycloak() -> None:
    if not port_open("127.0.0.1", 8180):
        pytest.skip("keycloak not running (make up)")


def mint(form: dict[str, str]) -> str:
    r = httpx.post(KC + "/protocol/openid-connect/token", data=form, timeout=10)
    if r.status_code == 401 and r.json().get("error") == "invalid_client":
        pytest.skip(
            f"client {form.get('client_id')} missing in the running realm; "
            "re-import with `make clean up` (realm.json is only imported on first start)"
        )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def dev1_token(keycloak: None) -> str:
    return mint(
        {
            "client_id": "mcp-gateway",
            "grant_type": "password",
            "username": "dev1",
            "password": "dev1",
        }
    )


@pytest.fixture(scope="module")
def ci_token(keycloak: None) -> str:
    return mint(
        {
            "client_id": "ci-runner",
            "client_secret": "ci-runner-secret",
            "grant_type": "client_credentials",
        }
    )


@pytest.fixture(scope="module")
def other_api_token(keycloak: None) -> str:
    return mint(
        {
            "client_id": "other-api",
            "client_secret": "other-api-secret",
            "grant_type": "client_credentials",
        }
    )


async def whoami(url: str, token: str | None) -> dict[str, Any]:
    async with Client(url + "/mcp", auth=token) as c:
        res = await c.call_tool("gateway_whoami", {})
        return res.data


async def test_dev1_token_is_accepted(base_url: str, dev1_token: str) -> None:  # noqa: F811
    p = await whoami(base_url, dev1_token)
    assert p["kind"] == "user"
    assert p["client_id"] == "mcp-gateway"
    assert "mcp-developer" in p["claims"]["realm_access"]["roles"]
    assert p["roles"] == p["claims"]["realm_access"]["roles"]
    assert p["sub"]
    # the same client without a token is a guest
    assert (await whoami(base_url, None))["kind"] == "anonymous"


async def test_ci_runner_client_credentials(base_url: str, ci_token: str) -> None:  # noqa: F811
    p = await whoami(base_url, ci_token)
    assert p["kind"] == "user"
    assert p["client_id"] == "ci-runner"
    assert p["claims"]["azp"] == "ci-runner"


def test_wrong_audience_is_401(base_url: str, other_api_token: str) -> None:  # noqa: F811
    r = httpx.post(
        base_url + "/mcp",
        json=TOOLS_LIST,
        headers={**HEADERS, "Authorization": f"Bearer {other_api_token}"},
        timeout=10,
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_garbage_bearer_is_401_with_challenge(base_url: str) -> None:  # noqa: F811
    r = httpx.post(
        base_url + "/mcp",
        json=TOOLS_LIST,
        headers={**HEADERS, "Authorization": "Bearer garbage"},
        timeout=10,
    )
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == (
        'Bearer resource_metadata="http://localhost:8080/.well-known/oauth-protected-resource", '
        'error="invalid_token"'
    )


def test_metadata_points_at_keycloak(base_url: str, keycloak: None) -> None:  # noqa: F811
    doc = httpx.get(base_url + "/.well-known/oauth-protected-resource").json()
    assert doc["authorization_servers"] == [KC]
    assert doc["resource"] == "http://localhost:8080/mcp"
    assert doc["bearer_methods_supported"] == ["header"]
    ready = httpx.get(base_url + "/readyz").json()
    assert ready["checks"]["oidc"]["jwks_uri"] == KC + "/protocol/openid-connect/certs"
