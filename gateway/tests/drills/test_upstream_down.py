"""Drill: one upstream dies and comes back while a client keeps calling.

Expected (PLAN WP4 done-when): only that upstream's tools fail, fast and as MCP errors
(never HTTP 5xx); the other upstream's tools and tools/list keep working;
``gateway_upstream_up`` follows the probe; calls recover on their own once the upstream
is back. The drill runs its own upstream so the shared stack is not touched.
"""

from __future__ import annotations

import time

import pytest
from fastmcp import FastMCP

from mcp_gateway.settings import StaticUpstream
from tests.conftest import free_port, port_open
from tests.drills.support import (
    call,
    call_ok,
    drill_settings,
    list_names,
    metric,
    running_gateway,
    serve_in_thread,
    wait_until,
)

pytestmark = pytest.mark.drill


def drill_upstream_app():
    mcp = FastMCP("drill-up")

    @mcp.tool
    def ping() -> str:
        return "pong"

    return mcp.http_app(path="/mcp", stateless_http=True)


def test_upstream_down_and_back_only_its_tools_fail() -> None:
    if not port_open("127.0.0.1", 8081):
        pytest.skip("sample-public on :8081 not running (make up)")
    port = free_port()
    upstream = serve_in_thread(drill_upstream_app(), port)
    settings = drill_settings(
        policy_source="static",
        upstreams_source="static",
        static_upstreams=[
            StaticUpstream(
                id="sample-public", url="http://localhost:8081/mcp", protocol_mode="2026-07-28"
            ),
            StaticUpstream(
                id="drill-up", url=f"http://127.0.0.1:{port}/mcp", protocol_mode="2026-07-28"
            ),
        ],
    )
    try:
        with running_gateway(settings) as (base, _gw):
            wait_until(
                lambda: metric(base, 'gateway_upstream_up{upstream="drill-up"}') == 1.0,
                10,
                what="drill-up probed up",
            )
            assert call_ok(call(base, "drill-up_ping", {}))
            five0 = metric(base, 'gateway_http_responses_total{code="5xx",route="mcp"}') or 0.0

            upstream.stop()  # the upstream dies
            assert not port_open("127.0.0.1", port)
            t0 = time.monotonic()
            msg = call(base, "drill-up_ping", {})
            took = time.monotonic() - t0
            assert not call_ok(msg), msg  # its tool fails ...
            assert took < 5, f"failed call took {took:.1f}s (connect timeout is 2 s)"
            assert call_ok(
                call(base, "sample-public_echo", {"text": "still here"})
            )  # ... others don't
            assert "sample-public_echo" in list_names(base)
            wait_until(
                lambda: metric(base, 'gateway_upstream_up{upstream="drill-up"}') == 0.0,
                10,
                what="drill-up probed down",
            )
            print(f"\n  upstream down: its call failed in {took * 1000:.0f} ms, others fine")

            upstream = serve_in_thread(drill_upstream_app(), port)  # and comes back
            took = wait_until(
                lambda: call_ok(call(base, "drill-up_ping", {})),
                15,
                0.2,
                what="drill-up calls recover",
            )
            wait_until(
                lambda: metric(base, 'gateway_upstream_up{upstream="drill-up"}') == 1.0,
                10,
                what="drill-up probed up again",
            )
            print(f"  upstream back: calls recovered after {took:.1f}s")
            five1 = metric(base, 'gateway_http_responses_total{code="5xx",route="mcp"}') or 0.0
            assert five1 == five0, "upstream failure must never surface as HTTP 5xx"
    finally:
        upstream.stop()
