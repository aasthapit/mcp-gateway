"""Mongo-backed policy against the local stack (``make up``): seed, run the gateway
in-process, change the allow-list, watch it propagate; then a replica-set step-down.

Needs Mongo on :27017 and the sample upstreams on :8081/:8082. Re-seeds the policy
collections from ``local/seed`` before and after, so it leaves the stack as ``make seed``
would.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client
from mcp.client.subscriptions import ToolsListChanged, listen
from pymongo import MongoClient, WriteConcern
from pymongo.errors import PyMongoError

from mcp_gateway.app import Gateway, build_gateway
from mcp_gateway.auth.principal import ANONYMOUS
from mcp_gateway.debug_tools import DEBUG_TOOL_NAMES
from mcp_gateway.middleware.metrics import POLICY_RELOADS
from mcp_gateway.settings import Settings
from tests.conftest import free_port, port_open

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
SEED = REPO / "local" / "seed"
MONGO_URI = "mongodb://root:devpass@localhost:27017/?replicaSet=rs0&directConnection=true"
DB = "mcp_gateway"
MAJORITY = WriteConcern(w="majority", wtimeout=10_000)

GUEST_TOOLS = {"sample-public_time_now", "sample-public_echo"}
PRIVATE_TOOL = "sample-private_secret_lookup"


# -- helpers -----------------------------------------------------------------------------


def seed(mongo: MongoClient[Any], actor: str = "pytest") -> int:
    """Same as local/seed/seed.js: upsert every seed document, bump meta.version."""
    db = mongo[DB]
    now = datetime.now(UTC)
    for name in ("upstreams", "toolsets", "grants"):
        coll = db.get_collection(name, write_concern=MAJORITY)
        docs = json.loads((SEED / f"{name}.json").read_text())
        coll.delete_many({"_id": {"$nin": [d["_id"] for d in docs]}})
        for doc in docs:
            coll.replace_one(
                {"_id": doc["_id"]}, {**doc, "updatedAt": now, "updatedBy": actor}, upsert=True
            )
    meta = db.get_collection("meta", write_concern=MAJORITY).find_one_and_update(
        {"_id": "policy"},
        {"$inc": {"version": 1}, "$set": {"updatedAt": now, "updatedBy": actor}},
        upsert=True,
        return_document=True,
    )
    return int(meta["version"])


def set_toolset_allow(mongo: MongoClient[Any], toolset: str, allow: list[str]) -> int:
    """What the admin API does: edit + version bump in one majority-committed transaction."""
    db = mongo[DB]
    now = datetime.now(UTC)
    with mongo.start_session() as s, s.start_transaction(write_concern=MAJORITY):
        db.toolsets.update_one(
            {"_id": toolset},
            {"$set": {"allow": allow, "updatedAt": now, "updatedBy": "pytest"}},
            session=s,
        )
        meta = db.meta.find_one_and_update(
            {"_id": "policy"},
            {"$inc": {"version": 1}, "$set": {"updatedAt": now, "updatedBy": "pytest"}},
            session=s,
            return_document=True,
        )
    return int(meta["version"])


async def guest_tools(base: str) -> set[str]:
    """Upstream tools the guest sees (dev-only gateway tools such as whoami excluded)."""
    async with Client(base + "/mcp", mode="2026-07-28") as c:
        return {t.name for t in await c.list_tools()} - DEBUG_TOOL_NAMES


async def wait_tools(base: str, predicate, timeout: float) -> tuple[set[str], float]:
    """Poll tools/list until ``predicate(names)``; returns the names and the time taken."""
    start = time.perf_counter()
    while True:
        names = await guest_tools(base)
        if predicate(names):
            return names, time.perf_counter() - start
        if time.perf_counter() - start > timeout:
            raise AssertionError(f"tools/list did not change within {timeout}s: {sorted(names)}")
        await asyncio.sleep(0.1)


def readyz(base: str) -> dict[str, Any]:
    return httpx.get(base + "/readyz", timeout=5).json()


def metric(text: str, sample: str) -> float | None:
    """Value of ``sample`` (name plus label set) in Prometheus text, or None."""
    for line in text.splitlines():
        if line.startswith(sample + " "):
            return float(line.split()[-1])
    return None


def wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


def wait_primary(mongo: MongoClient[Any], timeout: float = 30) -> None:
    def is_primary() -> bool:
        try:
            return bool(mongo.admin.command("hello").get("isWritablePrimary"))
        except PyMongoError:
            return False

    wait_for(is_primary, timeout, "replica set primary")


# -- fixtures ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack_available() -> None:
    if not (port_open("127.0.0.1", 8081) and port_open("127.0.0.1", 8082)):
        pytest.skip("local stack not running (make up): sample upstreams on 8081/8082")
    if not port_open("127.0.0.1", 27017):
        pytest.skip("local stack not running (make up): mongo on 27017")


@pytest.fixture(scope="module")
def mongo(stack_available: None) -> Iterator[MongoClient[Any]]:
    client: MongoClient[Any] = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5_000)
    seed(client)
    yield client
    seed(client)  # leave the stack as make seed would
    client.close()


@pytest.fixture(scope="module")
def gateway(mongo: MongoClient[Any]) -> Iterator[tuple[str, Gateway]]:
    settings = Settings(
        metrics_port=0,
        log_json=False,
        log_level="INFO",
        policy_source="mongo",
        policy_poll_sec=2,
    )
    gw = build_gateway(settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(gw.app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(base + "/readyz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        pytest.fail(f"gateway did not become ready: {readyz(base)}")
    yield base, gw
    server.should_exit = True
    thread.join(10)


# -- tests -------------------------------------------------------------------------------


async def test_guest_sees_only_public_basics(gateway: tuple[str, Gateway]) -> None:
    base, gw = gateway
    assert await guest_tools(base) == GUEST_TOOLS
    async with Client(base + "/mcp") as c:
        assert (await c.call_tool("sample-public_echo", {"text": "hi"})).data == "hi"
        with pytest.raises(Exception, match=f"Unknown tool: '{PRIVATE_TOOL}'"):
            await c.call_tool(PRIVATE_TOOL, {"key": "x"})
    assert gw.snapshot.matching_grants(ANONYMOUS) == ["anon-default"]


def test_readyz_and_metrics_report_policy(gateway: tuple[str, Gateway], mongo) -> None:
    base, gw = gateway
    detail = readyz(base)
    policy = detail["checks"]["policy"]
    assert policy["ok"] and policy["critical"]
    assert policy["version"] == mongo[DB].meta.find_one()["version"] == gw.snapshot.version
    assert policy["policy_age_seconds"] >= 0 and policy["change_stream_up"] is True
    assert detail["checks"]["policy_stream"] == {
        "ok": True,
        "critical": False,
        "change_stream_up": True,
        "error": None,
    }
    # The stream's first open (no resume token) asks for one reload to close the gap
    # between the initial load and the stream start; it lands just after readiness.
    stream_reloads = 'gateway_policy_reloads_total{reason="stream"}'
    wait_for(
        lambda: (metric(httpx.get(base + "/metrics").text, stream_reloads) or 0) >= 1,
        5,
        "stream (re)open reload",
    )
    text = httpx.get(base + "/metrics").text
    assert metric(text, "gateway_policy_version") == float(gw.snapshot.version)
    assert (metric(text, "gateway_policy_age_seconds") or 0) >= 0
    assert metric(text, "gateway_change_stream_up") == 1.0
    # counters are process-global: other in-process gateways in this run count too
    assert (metric(text, 'gateway_policy_reloads_total{reason="initial"}') or 0) >= 1


async def test_modern_discover_advertises_list_changed(gateway: tuple[str, Gateway]) -> None:
    base, _ = gateway
    r = httpx.post(
        base + "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "server/discover",
        },
        timeout=10,
    )
    assert r.json()["result"]["capabilities"]["tools"]["listChanged"] is True


async def test_toolset_edit_propagates_and_notifies_listeners(
    gateway: tuple[str, Gateway], mongo: MongoClient[Any]
) -> None:
    base, gw = gateway
    assert await guest_tools(base) == GUEST_TOOLS
    events: list[Any] = []
    acked = asyncio.Event()

    async def listener() -> None:
        async with (
            Client(base + "/mcp", mode="2026-07-28") as c,
            listen(c.session, tools_list_changed=True) as sub,
        ):
            acked.set()
            async for ev in sub:
                events.append(ev)
                if len(events) >= 2:
                    return

    task = asyncio.create_task(listener())
    await asyncio.wait_for(acked.wait(), 10)
    version_before = gw.snapshot.version
    original = mongo[DB].toolsets.find_one({"_id": "public-basics"})["allow"]
    try:
        new_version = set_toolset_allow(mongo, "public-basics", [*original, PRIVATE_TOOL])
        names, took = await wait_tools(base, lambda n: PRIVATE_TOOL in n, timeout=2.0)
        assert names == GUEST_TOOLS | {PRIVATE_TOOL}
        assert took < 2.0
        assert gw.snapshot.version == new_version > version_before
        async with Client(base + "/mcp") as c:
            assert (await c.call_tool(PRIVATE_TOOL, {"key": "x"})).data

        set_toolset_allow(mongo, "public-basics", original)
        names, took = await wait_tools(base, lambda n: PRIVATE_TOOL not in n, timeout=2.0)
        assert names == GUEST_TOOLS
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
        set_toolset_allow(mongo, "public-basics", original)
    assert len(events) == 2 and all(isinstance(e, ToolsListChanged) for e in events)
    text = httpx.get(base + "/metrics").text
    assert (
        'gateway_policy_denials_total{principal_kind="anonymous",tool="sample-private_secret_lookup"}'
        in text
    )


async def test_poll_fallback_reloads_when_the_stream_is_silenced(
    gateway: tuple[str, Gateway], mongo: MongoClient[Any]
) -> None:
    """Close the change stream's cursor from the outside so the edit is only seen by the
    2 s version poll (GATEWAY_POLICY_POLL_SEC)."""
    base, gw = gateway
    store = gw.policy_store
    polls_before = POLICY_RELOADS.labels(reason="poll")._value.get()
    original = mongo[DB].toolsets.find_one({"_id": "public-basics"})["allow"]
    try:
        # Kill every change-stream cursor the gateway holds. The stream loop sees the
        # cursor die, marks the stream down and reopens with its token after a backoff,
        # which also triggers a reload; so to prove the poll path we check its counter.
        for op in mongo.admin.aggregate([{"$currentOp": {"allUsers": True}}]):
            if "getMore" in op.get("command", {}) and "changeStream" in str(
                op.get("originatingCommand", "")
            ):
                with contextlib.suppress(PyMongoError):
                    mongo.admin.command("killOp", op=op["opid"])
        new_version = set_toolset_allow(mongo, "public-basics", [*original, PRIVATE_TOOL])
        await wait_tools(base, lambda n: PRIVATE_TOOL in n, timeout=4.0)
        assert gw.snapshot.version == new_version
        assert getattr(store, "loaded_version", new_version) == new_version
        wait_for(lambda: getattr(store, "stream_up", True), 15, "change stream back up")
    finally:
        set_toolset_allow(mongo, "public-basics", original)
        await wait_tools(base, lambda n: PRIVATE_TOOL not in n, timeout=4.0)
    assert POLICY_RELOADS.labels(reason="poll")._value.get() >= polls_before


async def test_replica_set_stepdown_then_edit_still_propagates(
    gateway: tuple[str, Gateway], mongo: MongoClient[Any]
) -> None:
    base, gw = gateway
    store = gw.policy_store
    resumes_before = getattr(store, "resumes", 0)
    reopens_before = getattr(store, "full_reopens", 0)
    assert await guest_tools(base) == GUEST_TOOLS

    mongo.admin.command({"replSetStepDown": 1, "force": True})
    wait_primary(mongo)
    # The gateway kept serving from its snapshot throughout.
    assert await guest_tools(base) == GUEST_TOOLS
    assert readyz(base)["ready"] is True
    wait_for(lambda: getattr(store, "stream_up", True), 15, "change stream back up")

    original = mongo[DB].toolsets.find_one({"_id": "public-basics"})["allow"]
    try:
        new_version = set_toolset_allow(mongo, "public-basics", [*original, PRIVATE_TOOL])
        await wait_tools(base, lambda n: PRIVATE_TOOL in n, timeout=5.0)
        assert gw.snapshot.version == new_version
    finally:
        set_toolset_allow(mongo, "public-basics", original)
        await wait_tools(base, lambda n: PRIVATE_TOOL not in n, timeout=5.0)
    status = readyz(base)["checks"]["policy"]
    assert status["change_stream_up"] is True
    # Either pymongo resumed transparently inside the cursor, or our loop reopened it
    # with the saved token; a full reopen (token dropped) would also be acceptable but is
    # not expected from a 1 s step-down.
    assert status["resumes"] >= resumes_before
    assert status["full_reopens"] == reopens_before
