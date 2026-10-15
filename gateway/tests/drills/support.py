"""Helpers for the failure drills: in-thread gateways and upstreams, a TCP fault proxy,
gateway subprocesses and a tiny round-robin HTTP load balancer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_gateway.app import Gateway, build_gateway
from mcp_gateway.debug_tools import DEBUG_TOOL_NAMES
from mcp_gateway.main import GatewayServer, server_config
from mcp_gateway.settings import Settings
from tests.conftest import free_port

GATEWAY_DIR = Path(__file__).resolve().parents[2]
REPO = GATEWAY_DIR.parent
CONTAINER = os.environ.get("CONTAINER", "podman")
MODERN = "2026-07-28"
GUEST_FLOOR = {"sample-public_time_now", "sample-public_echo"}


def drill_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "metrics_port": 0,
        "log_json": False,
        "log_level": "WARNING",
        "upstream_probe_sec": 0.5,
        "upstream_probe_timeout_sec": 2,
        "rate_limit_enabled": False,
        "shutdown_grace_sec": 10,
    }
    base.update(overrides)
    for key in list(os.environ):  # drills configure everything explicitly
        if key.startswith("GATEWAY_") and key not in ("GATEWAY_DRILLS",):
            os.environ.pop(key)
    return Settings(**base)


# -- raw MCP over HTTP (modern era, one POST per call) ------------------------------------


def mcp_post(
    base: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    timeout: float = 15,
    client: httpx.Client | None = None,
) -> httpx.Response:
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if "name" in params:
        headers["Mcp-Name"] = params["name"]
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    poster = client.post if client is not None else httpx.post
    return poster(base + "/mcp", json=body, headers=headers, timeout=timeout)


def rpc_message(r: httpx.Response) -> dict[str, Any]:
    raw = r.text
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        raw = [line[5:] for line in raw.splitlines() if line.startswith("data:")][-1]
    return json.loads(raw)


def list_names(base: str, **kw: Any) -> set[str]:
    """Tool names from tools/list, without the dev-only gateway tools (``gateway_whoami``)."""
    r = mcp_post(base, "tools/list", **kw)
    r.raise_for_status()
    return {
        t["name"] for t in rpc_message(r)["result"]["tools"] if t["name"] not in DEBUG_TOOL_NAMES
    }


def call(base: str, tool: str, arguments: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """The JSON-RPC message of a tools/call (``result`` or ``error``); raises on non-200."""
    r = mcp_post(base, "tools/call", {"name": tool, "arguments": arguments}, **kw)
    r.raise_for_status()
    return rpc_message(r)


def call_ok(msg: dict[str, Any]) -> bool:
    return "result" in msg and not msg["result"].get("isError")


def metric(base: str, line_prefix: str) -> float | None:
    for line in httpx.get(base + "/metrics", timeout=5).text.splitlines():
        if line.startswith(line_prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


def wait_until(pred: Any, timeout: float, interval: float = 0.1, what: str = "condition") -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        with contextlib.suppress(Exception):
            if pred():
                return time.monotonic() - t0
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# -- servers in threads ---------------------------------------------------------------------


@dataclass
class ThreadServer:
    server: uvicorn.Server
    thread: threading.Thread
    base: str

    def stop(self, timeout: float = 15) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


def serve_in_thread(
    app: Any, port: int | None = None, *, gateway: Gateway | None = None
) -> ThreadServer:
    port = port or free_port()
    if gateway is not None:
        cfg = server_config(gateway, gateway.settings, host="127.0.0.1", port=port)
        server: uvicorn.Server = GatewayServer(cfg, gateway)
    else:
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
        )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    wait_until(lambda: server.started, 20, 0.05, f"server on {base}")
    return ThreadServer(server, thread, base)


@contextlib.contextmanager
def running_gateway(settings: Settings, **build_kw: Any) -> Iterator[tuple[str, Gateway]]:
    gw = build_gateway(settings, **build_kw)
    ts = serve_in_thread(gw.app, gateway=gw)
    try:
        wait_until(
            lambda: httpx.get(ts.base + "/readyz", timeout=2).status_code == 200,
            30,
            what="gateway /readyz",
        )
        yield ts.base, gw
    finally:
        ts.stop()


# -- TCP fault proxy -------------------------------------------------------------------------


class FaultProxy:
    """TCP forwarder in its own event loop thread. ``mode``: ``up`` forwards, ``refuse``
    closes the listener (connection refused), ``blackhole`` accepts and never answers."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.port = free_port()
        self.mode = "up"
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._run(self._listen())

    def _run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(10)

    async def _listen(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            if self.mode == "blackhole":
                await asyncio.sleep(3600)
                return
            up_reader, up_writer = await asyncio.open_connection(*self.target)
            self._writers.add(up_writer)

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                with contextlib.suppress(Exception):
                    while data := await src.read(65536):
                        if self.mode != "up":
                            return
                        dst.write(data)
                        await dst.drain()
                with contextlib.suppress(Exception):
                    dst.close()

            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def set_mode(self, mode: str) -> None:
        async def apply() -> None:
            self.mode = mode
            for w in list(self._writers):  # cut existing (keep-alive) connections
                with contextlib.suppress(Exception):
                    w.close()
            self._writers.clear()
            if mode == "refuse" and self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            elif mode != "refuse" and self._server is None:
                await self._listen()

        self._run(apply())

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.set_mode("refuse")
        self._loop.call_soon_threadsafe(self._loop.stop)


# -- gateway subprocesses + round-robin balancer --------------------------------------------


@dataclass
class GatewayProcess:
    port: int
    env: dict[str, str]
    log_path: Path
    proc: subprocess.Popen[bytes] | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
        env.update(self.env)
        env["GATEWAY_PORT"] = str(self.port)
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            ["uv", "run", "--frozen", "mcp-gateway"],
            cwd=GATEWAY_DIR,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def ready(self) -> bool:
        try:
            return httpx.get(self.base + "/readyz", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def wait_ready(self, timeout: float = 60) -> None:
        wait_until(self.ready, timeout, 0.2, f"gateway :{self.port} ready")

    def terminate(self, timeout: float = 30) -> int:
        assert self.proc is not None
        # `uv run` forwards SIGTERM to the child (the gateway process).
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout)


@dataclass
class Backend:
    base: str
    in_rotation: bool = True
    ready: bool = False
    served: int = 0


@dataclass
class RoundRobinProxy:
    """What the OpenShift router + Endpoints do, in 80 lines: route each request to the
    next backend that is in rotation (pod not terminating) and ready (``/readyz`` polled
    every 250 ms); retry the next backend when the connection fails or the backend closes
    it without a response (HAProxy ``retry-on conn-failure empty-response``)."""

    backends: list[Backend]
    port: int = field(default_factory=free_port)
    retries: int = 0
    no_backend: int = 0
    _rr: int = 0

    def __post_init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=2))
        self.app = Starlette(
            routes=[Route("/{path:path}", self._forward, methods=["GET", "POST", "DELETE"])],
            lifespan=self._lifespan,
        )

    @contextlib.asynccontextmanager
    async def _lifespan(self, app: Starlette) -> Any:
        task = asyncio.create_task(self._health_loop())
        try:
            yield
        finally:
            task.cancel()
            await self.client.aclose()

    async def _health_loop(self) -> None:
        async with httpx.AsyncClient(timeout=1) as probe:
            while True:
                for b in self.backends:
                    try:
                        b.ready = (await probe.get(b.base + "/readyz")).status_code == 200
                    except httpx.HTTPError:
                        b.ready = False
                await asyncio.sleep(0.25)

    def _candidates(self) -> list[Backend]:
        live = [b for b in self.backends if b.in_rotation and b.ready]
        if not live:
            return []
        self._rr = (self._rr + 1) % len(live)
        return live[self._rr :] + live[: self._rr]

    async def _forward(self, request: Request) -> Response:
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
        }
        for backend in self._candidates():
            try:
                r = await self.client.request(
                    request.method,
                    backend.base + request.url.path,
                    content=body,
                    headers=headers,
                )
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                self.retries += 1
                continue
            backend.served += 1
            drop = {"content-length", "transfer-encoding", "connection", "content-encoding"}
            return Response(
                r.content,
                status_code=r.status_code,
                headers={k: v for k, v in r.headers.items() if k.lower() not in drop},
            )
        self.no_backend += 1
        return Response("no backend available", status_code=503)
