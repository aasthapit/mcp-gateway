"""Graceful shutdown: SIGTERM drains listen streams and lets in-flight calls finish.

Runs the real ``main.GatewayServer`` (uvicorn) in a thread on a free port; the signal is
delivered through ``Server.handle_exit(SIGTERM)``, which is exactly what uvicorn's own
signal handler calls (handlers can only be installed on the main thread). No upstreams
or Mongo needed: static allow-all policy and a slow tool registered on the gateway.
"""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time

import anyio
import httpx
import pytest
from fastmcp import Client
from mcp.client.subscriptions import SubscriptionLost, listen

from mcp_gateway.app import build_gateway
from mcp_gateway.main import GatewayServer, server_config
from mcp_gateway.settings import Settings
from tests.conftest import free_port

SLOW_SEC = 2.0


@pytest.fixture
def running(settings: Settings):
    s = settings.model_copy(
        update={"static_upstreams": [], "ready_requires_upstream": False, "shutdown_grace_sec": 10}
    )
    gw = build_gateway(s)
    started = threading.Semaphore(0)

    @gw.mcp.tool(name="slow_echo")
    async def slow_echo(text: str) -> str:
        started.release()
        await anyio.sleep(SLOW_SEC)
        return text

    port = free_port()
    server = GatewayServer(server_config(gw, s, host="127.0.0.1", port=port), gw)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/readyz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    else:
        pytest.fail("gateway did not become ready")
    yield base, gw, server, thread, started
    server.should_exit = True
    thread.join(15)


def _post_call(base: str, protocol_version: str, text: str) -> httpx.Response:
    """One raw ``tools/call`` POST: modern era answers JSON, legacy era answers SSE."""
    params: dict[str, object] = {"name": "slow_echo", "arguments": {"text": text}}
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
    }
    if protocol_version == "2026-07-28":
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": protocol_version,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        headers |= {"Mcp-Method": "tools/call", "Mcp-Name": "slow_echo"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return httpx.post(base + "/mcp", json=body, headers=headers, timeout=SLOW_SEC + 10)


def _result_text(r: httpx.Response) -> str:
    raw = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        raw = next(line[5:] for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(raw)["result"]["content"][0]["text"]


async def test_sigterm_drains_listen_stream_and_finishes_slow_calls(running) -> None:
    base, gw, server, thread, started = running
    acked = asyncio.Event()
    outcome: dict[str, object] = {}

    async def listener() -> None:
        try:
            async with (
                Client(base + "/mcp", mode="2026-07-28") as c,
                listen(c.session, tools_list_changed=True) as sub,
            ):
                acked.set()
                try:
                    async for _ in sub:  # ends with StopAsyncIteration on graceful close
                        pass
                    outcome["listen"] = "graceful"
                    outcome["listen_ended_at"] = time.monotonic()
                except SubscriptionLost as exc:
                    outcome["listen"] = f"lost: {exc}"
        except Exception as exc:
            # The client's own teardown may need a fresh connection, which a draining
            # server no longer accepts; the stream outcome above is what matters.
            outcome.setdefault("listen", f"error: {exc!r}")

    listen_task = asyncio.create_task(listener())
    await asyncio.wait_for(acked.wait(), 10)
    assert gw.listen_handler is not None and gw.listen_handler.open_streams == 1

    # Two slow calls in flight: modern era (JSON response) and legacy era (SSE response;
    # sse-starlette would cut it on SIGTERM unless its automatic drain is disabled).
    modern = asyncio.create_task(asyncio.to_thread(_post_call, base, "2026-07-28", "modern"))
    legacy = asyncio.create_task(asyncio.to_thread(_post_call, base, "2025-11-25", "legacy"))
    for _ in range(2):
        assert await asyncio.to_thread(started.acquire, True, 10), "slow tool never started"
    await asyncio.sleep(0.2)  # both requests are inside the tool now

    t0 = time.monotonic()
    server.handle_exit(signal.SIGTERM, None)

    # The listen stream ends first and gracefully; the slow calls still complete.
    await asyncio.wait_for(listen_task, 5)
    assert outcome["listen"] == "graceful"
    assert outcome["listen_ended_at"] - t0 < SLOW_SEC  # type: ignore[operator]
    assert gw.readiness.draining is True
    for task, text in ((modern, "modern"), (legacy, "legacy")):
        r = await asyncio.wait_for(task, SLOW_SEC + 10)
        assert r.status_code == 200
        assert _result_text(r) == text
    assert time.monotonic() - t0 >= SLOW_SEC - 0.5  # they really ran to completion

    # The server exits once the in-flight calls are done, well within the grace period.
    await asyncio.to_thread(thread.join, 10)
    assert not thread.is_alive()
    assert time.monotonic() - t0 < 10
    assert gw.listen_handler.open_streams == 0
    with pytest.raises(httpx.ConnectError):  # no longer accepting connections
        httpx.get(base + "/healthz", timeout=1)


async def test_readyz_turns_503_while_draining(settings: Settings) -> None:
    from starlette.testclient import TestClient

    gw = build_gateway(settings.model_copy(update={"static_upstreams": []}))
    with TestClient(gw.app) as c:
        gw.readiness.probes.clear()
        assert c.get("/readyz").status_code == 200
        gw.begin_drain()
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["draining"] is True
        gw.begin_drain()  # idempotent
