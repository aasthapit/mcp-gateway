"""Guest smoke check used by ``make smoke``: ``python -m mcp_gateway.smoke [URL]``.

With a URL, checks a running gateway (set ``GATEWAY_INTERNAL_TOKEN`` to also check
``/internal/catalog``). Without one, starts the gateway in-process on a free port against
the local stack (upstreams from Mongo by default, sample upstreams on 8081/8082) and checks
that, including the catalog. Exits non-zero on failure; prints one line per check.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
import threading
import time

import httpx
import uvicorn
from fastmcp import Client

#: Upstream tool names expected under some namespace (``public_echo`` with the WP1 static
#: config, ``sample-public_echo`` with the Mongo seed).
EXPECTED_SUFFIXES = {"time_now", "echo", "secret_lookup"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m  {msg}")


def _skip(msg: str) -> None:
    print(f"  skip  {msg}")


def _wait_ready(base: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/readyz", timeout=1)
            if r.status_code == 200:
                return
            last = r.text
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.2)
    raise SystemExit(f"gateway at {base} not ready after {timeout}s: {last}")


def _by_suffix(names: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for suffix in EXPECTED_SUFFIXES:
        matches = sorted(n for n in names if n.endswith("_" + suffix))
        if matches:
            found[suffix] = matches[0]
    return found


async def check(base: str, internal_token: str | None) -> None:
    _wait_ready(base)
    _ok(f"{base}/readyz is 200")
    async with Client(base + "/mcp") as c:
        names = {t.name for t in await c.list_tools()}
        found = _by_suffix(names)
        missing = EXPECTED_SUFFIXES - set(found)
        if missing:
            raise SystemExit(f"guest tools/list missing *_{sorted(missing)}; got {sorted(names)}")
        _ok(f"guest lists {sorted(found.values())} (protocol {c.protocol_version})")
        res = await c.call_tool(found["echo"], {"text": "smoke"})
        if res.data != "smoke":
            raise SystemExit(f"{found['echo']} returned {res.data!r}")
        _ok(f"guest can call {found['echo']}")
        if await c.list_resources() or await c.list_prompts():
            raise SystemExit("resources/prompts should be hidden")
        _ok("resources and prompts are hidden")

    if not internal_token:
        _skip("/internal/catalog (set GATEWAY_INTERNAL_TOKEN to check it)")
        return
    headers = {"X-Internal-Token": internal_token}
    if httpx.get(base + "/internal/catalog", timeout=10).status_code != 403:
        raise SystemExit("/internal/catalog without token should be 403")
    r = httpx.get(base + "/internal/catalog", headers=headers, timeout=10)
    if r.status_code != 200:
        raise SystemExit(f"/internal/catalog -> {r.status_code}: {r.text}")
    body = r.json()
    cat_names = {t["namespaced_name"] for t in body["tools"]}
    missing = EXPECTED_SUFFIXES - set(_by_suffix(cat_names))
    if missing:
        raise SystemExit(f"catalog missing *_{sorted(missing)}; got {sorted(cat_names)}")
    if any(not t["input_schema"] for t in body["tools"] if t["name"] == "echo"):
        raise SystemExit("catalog entry for echo has no input_schema")
    _ok(
        f"/internal/catalog lists {len(cat_names)} tools from {sorted(body['upstreams'])} (403 without token)"
    )
    h = httpx.get(base + "/internal/upstreams/health", headers=headers, timeout=10).json()[
        "upstreams"
    ]
    down = sorted(u for u, s in h.items() if s["status"] not in ("up", "unknown"))
    if down:
        raise SystemExit(f"upstreams not healthy: {down}")
    _ok(
        f"/internal/upstreams/health: {', '.join(f'{u}={s["status"]}' for u, s in sorted(h.items()))}"
    )


def main(argv: list[str]) -> None:
    if argv:
        asyncio.run(check(argv[0].rstrip("/"), os.environ.get("GATEWAY_INTERNAL_TOKEN")))
        return
    from mcp_gateway.app import build_gateway
    from mcp_gateway.settings import load_settings

    token = os.environ.get("GATEWAY_INTERNAL_TOKEN") or secrets.token_urlsafe(16)
    port = _free_port()
    gw = build_gateway(
        load_settings(
            metrics_port=0, log_level="WARNING", internal_token=token, upstream_probe_sec=1
        )
    )
    print(f"  info  in-process gateway, upstreams from {gw.settings.upstreams_source}")
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
