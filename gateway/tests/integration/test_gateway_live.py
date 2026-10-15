"""Needs the local stack (``make up``): sample upstreams on localhost:8081 and :8082."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastmcp import Client

from mcp_gateway.app import build_gateway
from mcp_gateway.settings import Settings
from tests.conftest import free_port, port_open

pytestmark = pytest.mark.integration

ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture(scope="module")
def stack_available() -> None:
    if not (port_open("127.0.0.1", 8081) and port_open("127.0.0.1", 8082)):
        pytest.skip("local stack not running (make up): sample upstreams on 8081/8082")


@pytest.fixture(scope="module")
def base_url(stack_available: None) -> Iterator[str]:
    settings = Settings(
        metrics_port=0,
        log_json=False,
        log_level="WARNING",
        upstreams_source="static",  # WP1 behaviour; the Mongo path is tests/integration/test_upstreams_live.py
        upstream_probe_sec=1,
    )
    gw = build_gateway(settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(gw.app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/readyz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        pytest.fail("gateway did not become ready")
    yield base
    server.should_exit = True
    thread.join(10)


@pytest.mark.parametrize("mode", ["auto", "legacy", "2026-07-28"])
async def test_guest_lists_and_calls_through_gateway(base_url: str, mode: str) -> None:
    async with Client(base_url + "/mcp", mode=mode) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"public_time_now", "public_echo", "private_secret_lookup"} <= names
        res = await c.call_tool("public_echo", {"text": "hello"})
        assert res.data == "hello"
        assert await c.list_resources() == []
        assert await c.list_prompts() == []
        with pytest.raises(Exception, match="Unknown tool"):
            await c.call_tool("public_missing", {})


def test_modern_tools_list_carries_cache_hints(base_url: str) -> None:
    r = httpx.post(
        base_url + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": ENVELOPE}},
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        timeout=10,
    )
    result = r.json()["result"]
    assert result["ttlMs"] == 30_000
    assert result["cacheScope"] == "private"
    assert {t["name"] for t in result["tools"]} >= {"public_echo"}


def test_health_metrics_and_metadata(base_url: str) -> None:
    assert httpx.get(base_url + "/healthz").json() == {"status": "ok"}
    assert httpx.get(base_url + "/readyz").status_code == 200
    text = httpx.get(base_url + "/metrics").text
    assert (
        'gateway_tool_calls_total{outcome="ok",principal_kind="anonymous",tool="public_echo",upstream="public"}'
        in text
    )
    for _ in range(50):  # the first probe runs right after startup; give it a moment
        if 'gateway_upstream_up{upstream="private"} 1.0' in text:
            break
        time.sleep(0.1)
        text = httpx.get(base_url + "/metrics").text
    assert 'gateway_upstream_up{upstream="private"} 1.0' in text
    assert "gateway_upstreams_mounted 2.0" in text
    assert (
        httpx.get(base_url + "/.well-known/oauth-protected-resource")
        .json()["resource"]
        .endswith("/mcp")
    )
