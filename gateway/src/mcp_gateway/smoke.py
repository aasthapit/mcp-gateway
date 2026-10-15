"""Guest smoke check used by ``make smoke``: ``python -m mcp_gateway.smoke [URL]``.

With a URL, checks a running gateway. Without one, starts the gateway in-process on a free
port against the local stack (sample upstreams on 8081/8082) and checks that.
Exits non-zero on failure; prints one line per check.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time

import httpx
import uvicorn
from fastmcp import Client

EXPECTED_TOOLS = {"public_time_now", "public_echo", "private_secret_lookup"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m  {msg}")


def _wait_ready(base: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base + "/readyz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"gateway at {base} not ready after {timeout}s")


async def check(base: str) -> None:
    _wait_ready(base)
    _ok(f"{base}/readyz is 200")
    async with Client(base + "/mcp") as c:
        names = {t.name for t in await c.list_tools()}
        missing = EXPECTED_TOOLS - names
        if missing:
            raise SystemExit(f"guest tools/list missing {sorted(missing)}; got {sorted(names)}")
        _ok(f"guest lists {sorted(EXPECTED_TOOLS)} (protocol {c.protocol_version})")
        res = await c.call_tool("public_echo", {"text": "smoke"})
        if res.data != "smoke":
            raise SystemExit(f"public_echo returned {res.data!r}")
        _ok("guest can call public_echo")
        if await c.list_resources() or await c.list_prompts():
            raise SystemExit("resources/prompts should be hidden")
        _ok("resources and prompts are hidden")


def main(argv: list[str]) -> None:
    if argv:
        asyncio.run(check(argv[0].rstrip("/")))
        return
    from mcp_gateway.app import build_gateway
    from mcp_gateway.settings import load_settings

    port = _free_port()
    gw = build_gateway(load_settings(metrics_port=0, log_level="WARNING"))
    server = uvicorn.Server(
        uvicorn.Config(gw.app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        asyncio.run(check(f"http://127.0.0.1:{port}"))
    finally:
        server.should_exit = True
        thread.join(10)


if __name__ == "__main__":
    main(sys.argv[1:])
