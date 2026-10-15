"""Smoke check used by ``make smoke``: ``python -m mcp_gateway.smoke [URL]``.

With a URL, checks a running gateway. Without one, starts the gateway in-process on a free
port against the local stack (sample upstreams on 8081/8082, Keycloak on 8180) and checks
that. Flows:

* guest (no token): lists the sample tools, calls ``public_echo``;
* garbage bearer: 401 with the RFC 9728 ``WWW-Authenticate`` challenge;
* ``GATEWAY_SMOKE_TOKEN`` (the Makefile passes ``make token`` = dev1): the same client
  with the bearer, then the dev-only ``gateway_whoami`` tool prints the principal.

Exits non-zero on failure; prints one line per check.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from typing import Any

import httpx
import uvicorn
from fastmcp import Client

EXPECTED_TOOLS = {"public_time_now", "public_echo", "private_secret_lookup"}
MODERN_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/list",
}
TOOLS_LIST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}


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


def _principal_line(p: dict[str, Any]) -> str:
    return (
        f"kind={p.get('kind')} sub={p.get('sub')} client_id={p.get('client_id')} "
        f"roles={p.get('roles')} scopes={p.get('scopes')}"
    )


async def check(base: str, token: str | None) -> None:
    _wait_ready(base)
    _ok(f"{base}/readyz is 200")
    meta = httpx.get(base + "/.well-known/oauth-protected-resource", timeout=5).json()
    _ok(f"RFC 9728 metadata: resource={meta['resource']} as={meta['authorization_servers']}")

    r = httpx.post(
        base + "/mcp",
        json=TOOLS_LIST,
        headers={**MODERN_HEADERS, "Authorization": "Bearer garbage"},
        timeout=5,
    )
    challenge = r.headers.get("www-authenticate", "")
    if r.status_code != 401 or 'error="invalid_token"' not in challenge:
        raise SystemExit(
            f"garbage bearer: expected 401 invalid_token, got {r.status_code} {challenge!r}"
        )
    _ok(f"garbage bearer -> 401, WWW-Authenticate: {challenge}")

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
        if "gateway_whoami" in names:
            p = (await c.call_tool("gateway_whoami", {})).data
            if p.get("kind") != "anonymous":
                raise SystemExit(f"guest principal should be anonymous: {p}")
            _ok(f"guest principal: {_principal_line(p)}")

    if not token:
        print(
            "  skip  no GATEWAY_SMOKE_TOKEN: dev1 flow not checked (make smoke passes `make token`)"
        )
        return
    async with Client(base + "/mcp", auth=token) as c:
        names = {t.name for t in await c.list_tools()}
        missing = EXPECTED_TOOLS - names
        if missing:
            raise SystemExit(f"dev1 tools/list missing {sorted(missing)}; got {sorted(names)}")
        _ok(f"dev1 lists {sorted(names)}")
        if "gateway_whoami" not in names:
            print("  skip  gateway_whoami not exposed (GATEWAY_ENV!=dev): principal not printed")
            return
        p = (await c.call_tool("gateway_whoami", {})).data
        if p.get("kind") != "user":
            raise SystemExit(f"dev1 principal should be a user: {p}")
        _ok(f"dev1 principal: {_principal_line(p)}")


def main(argv: list[str]) -> None:
    token = os.environ.get("GATEWAY_SMOKE_TOKEN") or None
    if argv:
        asyncio.run(check(argv[0].rstrip("/"), token))
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
        asyncio.run(check(f"http://127.0.0.1:{port}", token))
    finally:
        server.should_exit = True
        thread.join(10)


if __name__ == "__main__":
    main(sys.argv[1:])
