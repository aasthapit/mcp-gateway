"""WP4 against the local stack (``make up`` + ``make seed``): Mongo on :27017, sample
upstreams on :8081/:8082. The stop/start tests also need the ``podman`` CLI and skip
without it. The gateway here reads its upstreams from the ``upstreams`` collection."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client
from pymongo import MongoClient

from mcp_gateway.app import Gateway, build_gateway
from mcp_gateway.settings import Settings, StaticUpstream
from tests.conftest import free_port, port_open

pytestmark = pytest.mark.integration

MONGO_URI = "mongodb://root:devpass@127.0.0.1:27017/?replicaSet=rs0&directConnection=true"
DB = "mcp_gateway"
TOKEN = "integration-token"
H = {"X-Internal-Token": TOKEN}
SEED = Path(__file__).resolve().parents[3] / "local" / "seed" / "upstreams.json"
PRIVATE_CONTAINER = "mcp-sample-private"
PROBE_SEC = 1.0


def wait_for(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.2)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture(scope="module")
def stack_available() -> None:
    if not (
        port_open("127.0.0.1", 8081)
        and port_open("127.0.0.1", 8082)
        and port_open("127.0.0.1", 27017)
    ):
        pytest.skip("local stack not running (make up): need :8081, :8082 and Mongo :27017")


@pytest.fixture(scope="module")
def mongo(stack_available: None) -> Iterator[MongoClient]:
    client: MongoClient = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    # Seed exactly what `make seed` loads (URLs point at localhost so a host-run gateway
    # reaches the containers), so this module does not depend on the user having seeded.
    docs = json.loads(SEED.read_text())
    coll = client[DB]["upstreams"]
    for doc in docs:
        coll.replace_one({"_id": doc["_id"]}, {**doc, "updatedBy": "pytest"}, upsert=True)
    yield client
    coll.delete_one({"_id": "sample-third"})
    client.close()


def run_gateway(settings: Settings) -> tuple[Gateway, str, Callable[[], None]]:
    gw = build_gateway(settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(gw.app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def ready() -> bool:
        try:
            return httpx.get(base + "/readyz", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    wait_for(ready, 20, "gateway readiness")

    def stop() -> None:
        server.should_exit = True
        thread.join(10)

    return gw, base, stop


@pytest.fixture(scope="module")
def gateway(mongo: MongoClient) -> Iterator[tuple[Gateway, str]]:
    gw, base, stop = run_gateway(
        Settings(
            metrics_port=0,
            log_json=False,
            log_level="WARNING",
            upstreams_source="mongo",
            mongo_uri=MONGO_URI,
            mongo_db=DB,
            internal_token=TOKEN,
            upstream_probe_sec=PROBE_SEC,
            upstream_probe_timeout_sec=3,
        )
    )
    yield gw, base
    stop()


def health(base: str) -> dict[str, Any]:
    return httpx.get(base + "/internal/upstreams/health", headers=H, timeout=5).json()["upstreams"]


def catalog_names(base: str) -> set[str]:
    return {
        t["namespaced_name"]
        for t in httpx.get(base + "/internal/catalog", headers=H, timeout=10).json()["tools"]
    }


async def list_names(base: str) -> set[str]:
    async with Client(base + "/mcp") as c:
        return {t.name for t in await c.list_tools()}


def podman() -> str:
    exe = shutil.which("podman")
    if exe is None:
        pytest.skip("podman CLI not available for stop/start tests")
    return exe


# -- both sample upstreams come from Mongo ----------------------------------------------------


async def test_upstreams_mount_from_mongo(gateway: tuple[Gateway, str]) -> None:
    gw, base = gateway
    assert gw.settings.upstreams_source == "mongo"
    assert set(gw.registry.namespaces) == {"sample-public", "sample-private"}
    assert await list_names(base) >= {
        "sample-public_time_now",
        "sample-public_echo",
        "sample-private_secret_lookup",
    }
    async with Client(base + "/mcp") as c:
        assert (await c.call_tool("sample-public_echo", {"text": "via mongo"})).data == "via mongo"
        res = await c.call_tool("sample-private_secret_lookup", {"key": "launch-code"})
        assert res.data == "0000-0000-0000"


def test_catalog_and_health(gateway: tuple[Gateway, str]) -> None:
    _, base = gateway
    body = httpx.get(base + "/internal/catalog", headers=H, timeout=10).json()
    assert sorted(body["upstreams"]) == ["sample-private", "sample-public"]
    by_name = {t["namespaced_name"]: t for t in body["tools"]}
    assert set(by_name) == {
        "sample-public_time_now",
        "sample-public_echo",
        "sample-private_secret_lookup",
    }
    echo = by_name["sample-public_echo"]
    assert echo["upstream"] == "sample-public" and echo["name"] == "echo"
    assert "text" in echo["input_schema"]["properties"]
    assert echo["description"]

    wait_for(
        lambda: all(h["status"] == "up" for h in health(base).values()), 10, "both upstreams up"
    )
    h = health(base)
    assert h["sample-public"]["tool_count"] == 2 and h["sample-private"]["tool_count"] == 1
    assert h["sample-public"]["session_mode"] == "shared"  # seed pins protocolMode 2026-07-28
    assert httpx.get(base + "/internal/catalog").status_code == 403
    readyz = httpx.get(base + "/readyz").json()
    assert readyz["checks"]["upstreams"]["mounted"] == ["sample-private", "sample-public"]
    text = httpx.get(base + "/metrics").text
    assert 'gateway_upstream_up{upstream="sample-private"} 1.0' in text
    assert "gateway_upstreams_mounted 2.0" in text


def test_effective_tools_for_anonymous(gateway: tuple[Gateway, str]) -> None:
    _, base = gateway
    r = httpx.get(base + "/internal/effective-tools?kind=anonymous", headers=H, timeout=10)
    assert r.status_code == 200  # AllowAllSnapshot until WP3 lands; 501 for a bare snapshot
    assert "sample-public_echo" in r.json()["tools"]


# -- a dead upstream ------------------------------------------------------------------------------


async def test_private_upstream_down_and_recovery(gateway: tuple[Gateway, str]) -> None:
    exe = podman()
    _, base = gateway
    subprocess.run([exe, "stop", "-t", "2", PRIVATE_CONTAINER], check=True, capture_output=True)
    try:
        wait_for(lambda: not port_open("127.0.0.1", 8082), 15, "port 8082 to close")
        started = time.time()
        wait_for(
            lambda: health(base)["sample-private"]["status"] == "down",
            PROBE_SEC + 3 + 2,  # one interval + probe timeout + slack
            "probe to report sample-private down",
        )
        down = health(base)["sample-private"]
        assert down["error"] and down["up"] is False
        assert time.time() - started < PROBE_SEC + 3 + 2

        # Readiness is unaffected, the public tools still list, the private call fails clearly.
        assert httpx.get(base + "/readyz").status_code == 200
        names = await list_names(base)
        assert {"sample-public_time_now", "sample-public_echo"} <= names
        async with Client(base + "/mcp") as c:
            assert (
                await c.call_tool("sample-public_echo", {"text": "still up"})
            ).data == "still up"
            # shared mode (seed pins protocolMode 2026-07-28): an already-connected shared
            # client reports "Connection closed"; a fresh one reports a connect failure.
            with pytest.raises(Exception, match=r"connect|closed|Unknown tool|unavailable") as exc:
                await c.call_tool("sample-private_secret_lookup", {"key": "launch-code"})
            assert "launch-code" not in str(exc.value)  # no argument echo in the error
        text = httpx.get(base + "/metrics").text
        assert 'gateway_upstream_up{upstream="sample-private"} 0.0' in text
        assert 'gateway_upstream_up{upstream="sample-public"} 1.0' in text
    finally:
        subprocess.run([exe, "start", PRIVATE_CONTAINER], check=True, capture_output=True)

    wait_for(lambda: port_open("127.0.0.1", 8082), 30, "port 8082 to open")
    wait_for(
        lambda: health(base)["sample-private"]["status"] == "up", 20, "sample-private to recover"
    )
    async with Client(base + "/mcp") as c:
        res = await c.call_tool("sample-private_secret_lookup", {"key": "launch-code"})
        assert res.data == "0000-0000-0000"


# -- live mount of a new document --------------------------------------------------------------------


async def test_third_upstream_mounts_live(gateway: tuple[Gateway, str], mongo: MongoClient) -> None:
    _, base = gateway
    coll = mongo[DB]["upstreams"]
    coll.replace_one(
        {"_id": "sample-third"},
        {
            "_id": "sample-third",
            "displayName": "Third (public again)",
            "enabled": True,
            "transport": "http",
            "url": "http://localhost:8081/mcp",
            "auth": {"type": "none"},
            "sessionMode": "per_request",
            "timeoutsMs": {"connect": 1000, "call": 10000},
            "cacheTtlSec": 0,
            "tags": ["test"],
        },
        upsert=True,
    )
    try:
        r = httpx.post(base + "/internal/upstreams/reload", headers=H, timeout=20)
        assert r.status_code == 200 and "sample-third" in r.json()["upstreams"]
        assert "sample-third_echo" in catalog_names(base)
        assert "sample-third_echo" in await list_names(base)
        async with Client(base + "/mcp") as c:
            assert (await c.call_tool("sample-third_echo", {"text": "3"})).data == "3"
        wait_for(lambda: health(base)["sample-third"]["status"] == "up", 10, "third upstream probe")

        # Disable it: unmounted live, tools gone, metric series dropped.
        coll.update_one({"_id": "sample-third"}, {"$set": {"enabled": False}})
        r = httpx.post(base + "/internal/upstreams/reload", headers=H, timeout=20)
        assert r.status_code == 200 and "sample-third" not in r.json()["upstreams"]
        assert "sample-third_echo" not in await list_names(base)
        async with Client(base + "/mcp") as c:
            with pytest.raises(Exception, match="Unknown tool"):
                await c.call_tool("sample-third_echo", {"text": "3"})
        assert (
            'upstream="sample-third"} '
            not in httpx.get(base + "/metrics").text.split("gateway_upstream_up")[1][:400]
        )
    finally:
        coll.delete_one({"_id": "sample-third"})
        httpx.post(base + "/internal/upstreams/reload", headers=H, timeout=20)


# -- shared session mode (static config, pinned modern era) -------------------------------------------


async def test_shared_session_reuses_connection_and_survives_restart(stack_available: None) -> None:
    exe = podman()
    gw, base, stop = run_gateway(
        Settings(
            metrics_port=0,
            log_json=False,
            log_level="WARNING",
            upstreams_source="static",
            static_upstreams=[
                StaticUpstream(
                    id="shared",
                    url="http://localhost:8082/mcp",
                    protocol_mode="2026-07-28",
                    session_mode="shared",
                )
            ],
            internal_token=TOKEN,
            upstream_probe_sec=PROBE_SEC,
            upstream_probe_timeout_sec=3,
        )
    )
    try:
        entry = gw.registry.mounted["shared"]
        assert entry.built is not None and entry.built.session_mode == "shared"
        shared = entry.built.shared
        assert shared is not None and not shared.is_connected()
        async with Client(base + "/mcp") as c:
            for _ in range(3):
                assert (
                    await c.call_tool("shared_secret_lookup", {"key": "launch-code"})
                ).data == "0000-0000-0000"
        assert shared.is_connected() and shared.reconnects == 1
        assert health(base)["shared"]["session_mode"] == "shared"

        subprocess.run([exe, "stop", "-t", "2", PRIVATE_CONTAINER], check=True, capture_output=True)
        try:
            wait_for(lambda: not port_open("127.0.0.1", 8082), 15, "port 8082 to close")
            async with Client(base + "/mcp") as c:
                with pytest.raises(Exception, match=r"connect|closed|refused|unavailable"):
                    await c.call_tool("shared_secret_lookup", {"key": "launch-code"})
        finally:
            subprocess.run([exe, "start", PRIVATE_CONTAINER], check=True, capture_output=True)
        wait_for(lambda: port_open("127.0.0.1", 8082), 30, "port 8082 to open")
        wait_for(lambda: health(base)["shared"]["status"] == "up", 20, "shared upstream to recover")

        async def call_ok() -> bool:
            async with Client(base + "/mcp") as c:
                try:
                    return (
                        await c.call_tool("shared_secret_lookup", {"key": "launch-code"})
                    ).data == "0000-0000-0000"
                except Exception:
                    return False

        for _ in range(20):
            if await call_ok():
                break
            time.sleep(0.5)
        else:
            pytest.fail("shared client did not recover after the upstream restarted")
        assert shared.reconnects >= 2
    finally:
        stop()
