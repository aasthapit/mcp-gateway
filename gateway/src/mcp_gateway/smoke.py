"""Smoke check used by ``make smoke``: ``python -m mcp_gateway.smoke [URL]``.

With a URL, checks a running gateway. Without one, starts the gateway in-process on a free
port against the local stack (sample upstreams on 8081/8082, Mongo on 27017, Keycloak on
8180) and checks that. Expects the ``local/seed`` policy (``make seed``). Exits non-zero on
failure; prints one line per check.

Checks: readiness and the RFC 9728 metadata; a garbage bearer gets 401 with the
``WWW-Authenticate`` challenge; the guest sees exactly the ``public-basics`` toolset and
cannot call a private tool, resources/prompts are hidden; then a live allow-list change: a
``subscriptions/listen`` stream is opened, ``public-basics`` is edited in Mongo (with the
``meta.version`` bump an admin write does, ``w: majority``), the next ``tools/list`` must
reflect it within 2 s and the stream must carry ``notifications/tools/list_changed``; the
edit is reverted the same way; the policy metrics are printed. Finally, with
``GATEWAY_SMOKE_TOKEN`` (the Makefile passes ``make token`` = dev1), the same client with
the bearer must see the ``engineering`` toolset on top of the guest floor, and the dev-only
``gateway_whoami`` tool prints the principal.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import uvicorn
from fastmcp import Client
from mcp.client.subscriptions import ToolsListChanged, listen
from pymongo import MongoClient, WriteConcern

GUEST_TOOLS = {"sample-public_time_now", "sample-public_echo"}
PRIVATE_TOOL = "sample-private_secret_lookup"
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
TOOLSET = "public-basics"
PROPAGATION_BUDGET_SEC = 2.0
MAJORITY = WriteConcern(w="majority", wtimeout=10_000)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m  {msg}")


def _wait_ready(base: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/readyz", timeout=1)
            last = r.json()
            if r.status_code == 200:
                return last
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"gateway at {base} not ready after {timeout}s: {last}")


def _mongo() -> tuple[MongoClient[Any], str]:
    from mcp_gateway.settings import load_settings

    s = load_settings()
    return MongoClient(s.mongo_uri, serverSelectionTimeoutMS=5_000), s.mongo_db


def _set_allow(mongo: MongoClient[Any], db_name: str, allow: list[str]) -> int:
    """An admin write: toolset edit + meta.version bump, one majority-committed transaction."""
    db = mongo[db_name]
    now = datetime.now(UTC)
    with mongo.start_session() as s, s.start_transaction(write_concern=MAJORITY):
        db.toolsets.update_one(
            {"_id": TOOLSET},
            {"$set": {"allow": allow, "updatedAt": now, "updatedBy": "smoke"}},
            session=s,
        )
        meta = db.meta.find_one_and_update(
            {"_id": "policy"},
            {"$inc": {"version": 1}, "$set": {"updatedAt": now, "updatedBy": "smoke"}},
            session=s,
            return_document=True,
        )
    return int(meta["version"])


async def _tools(base: str) -> set[str]:
    async with Client(base + "/mcp", mode="2026-07-28") as c:
        return {t.name for t in await c.list_tools()}


async def _wait_tools(base: str, predicate: Any, what: str) -> float:
    start = time.perf_counter()
    while True:
        names = await _tools(base)
        took = time.perf_counter() - start
        if predicate(names):
            return took
        if took > PROPAGATION_BUDGET_SEC:
            raise SystemExit(
                f"{what} not reflected within {PROPAGATION_BUDGET_SEC}s: {sorted(names)}"
            )
        await asyncio.sleep(0.1)


def _metric(text: str, sample: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(sample + " "):
            return line.split()[-1]
    return None


def _principal_line(p: dict[str, Any]) -> str:
    return (
        f"kind={p.get('kind')} sub={p.get('sub')} client_id={p.get('client_id')} "
        f"roles={p.get('roles')} scopes={p.get('scopes')}"
    )


async def check(base: str, token: str | None) -> None:
    ready = _wait_ready(base)
    policy = ready.get("checks", {}).get("policy", {})
    _ok(
        f"{base}/readyz is 200 (policy version {policy.get('version')}, "
        f"age {policy.get('policy_age_seconds')}s, change stream up={policy.get('change_stream_up')})"
    )
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
        if names != GUEST_TOOLS:
            raise SystemExit(
                f"guest tools/list should be exactly {sorted(GUEST_TOOLS)}; got {sorted(names)}"
            )
        _ok(f"guest lists exactly {sorted(GUEST_TOOLS)} (protocol {c.protocol_version})")
        res = await c.call_tool("sample-public_echo", {"text": "smoke"})
        if res.data != "smoke":
            raise SystemExit(f"sample-public_echo returned {res.data!r}")
        _ok("guest can call sample-public_echo")
        try:
            await c.call_tool(PRIVATE_TOOL, {"key": "x"})
        except Exception as exc:
            if "Unknown tool" not in str(exc):
                raise SystemExit(
                    f"{PRIVATE_TOOL} should answer 'Unknown tool', got: {exc}"
                ) from exc
            _ok(f"guest calling {PRIVATE_TOOL} gets 'Unknown tool' (existence not leaked)")
        else:
            raise SystemExit(f"guest could call {PRIVATE_TOOL}")
        if await c.list_resources() or await c.list_prompts():
            raise SystemExit("resources/prompts should be hidden")
        _ok("resources and prompts are hidden")
        if "gateway_whoami" in names:
            p = (await c.call_tool("gateway_whoami", {})).data
            if p.get("kind") != "anonymous":
                raise SystemExit(f"guest principal should be anonymous: {p}")
            _ok(f"guest principal: {_principal_line(p)}")

    # -- live allow-list change -------------------------------------------------------
    mongo, db_name = _mongo()
    doc = mongo[db_name].toolsets.find_one({"_id": TOOLSET})
    if doc is None:
        raise SystemExit(f"toolset {TOOLSET!r} missing in Mongo: run make seed")
    original: list[str] = list(doc["allow"])
    events: list[Any] = []
    acked = asyncio.Event()

    async def listener() -> None:
        async with (
            Client(base + "/mcp", mode="2026-07-28") as lc,
            listen(lc.session, tools_list_changed=True) as sub,
        ):
            acked.set()
            async for ev in sub:
                events.append(ev)
                if len(events) >= 2:
                    return

    task = asyncio.create_task(listener())
    await asyncio.wait_for(acked.wait(), 10)
    _ok("subscriptions/listen stream open (toolsListChanged)")
    try:
        version = _set_allow(mongo, db_name, [*original, PRIVATE_TOOL])
        took = await _wait_tools(base, lambda n: PRIVATE_TOOL in n, "toolset edit")
        _ok(
            f"edited toolset {TOOLSET} (meta.version {version}); guest sees {PRIVATE_TOOL} after {took:.2f}s"
        )
        version = _set_allow(mongo, db_name, original)
        took = await _wait_tools(base, lambda n: PRIVATE_TOOL not in n, "toolset revert")
        _ok(
            f"reverted toolset (meta.version {version}); {PRIVATE_TOOL} hidden again after {took:.2f}s"
        )
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
        _set_allow(mongo, db_name, original)
        mongo.close()
    if len(events) != 2 or not all(isinstance(e, ToolsListChanged) for e in events):
        raise SystemExit(f"expected 2 tools/list_changed notifications, got {events}")
    _ok("listen stream received notifications/tools/list_changed for both edits")

    text = httpx.get(base + "/metrics", timeout=5).text
    version_m = _metric(text, "gateway_policy_version")
    age_m = _metric(text, "gateway_policy_age_seconds")
    stream_m = _metric(text, "gateway_change_stream_up")
    if version_m is None or age_m is None or stream_m != "1.0":
        raise SystemExit(
            f"policy metrics missing: version={version_m} age={age_m} change_stream_up={stream_m}"
        )
    _ok(
        f"metrics: gateway_policy_version={version_m} gateway_policy_age_seconds={age_m} gateway_change_stream_up={stream_m}"
    )

    # -- dev1: bearer token, engineering toolset on top of the guest floor ------------
    if not token:
        print(
            "  skip  no GATEWAY_SMOKE_TOKEN: dev1 flow not checked (make smoke passes `make token`)"
        )
        return
    async with Client(base + "/mcp", auth=token) as c:
        names = {t.name for t in await c.list_tools()}
        missing = GUEST_TOOLS - names
        if missing:
            raise SystemExit(f"dev1 tools/list missing {sorted(missing)}; got {sorted(names)}")
        if PRIVATE_TOOL not in names:
            raise SystemExit(
                f"dev1 should get {PRIVATE_TOOL} via the engineering toolset; got {sorted(names)}"
            )
        _ok(f"dev1 lists {sorted(n for n in names if n != 'gateway_whoami')}")
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
    gw = build_gateway(
        load_settings(
            metrics_port=0,
            log_level=os.environ.get("GATEWAY_LOG_LEVEL", "WARNING"),
            policy_source="mongo",
        )
    )
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
