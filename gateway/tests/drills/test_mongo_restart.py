"""Drill: the Mongo primary restarts while guests keep listing tools.

Uses a throwaway single-node replica set container (``mcp-drill-mongo`` on :27018, seeded
from ``local/seed``) so the shared stack's Mongo is never restarted. Expected (PLAN WP3
chaos test, ADR 0008): the in-memory snapshot keeps serving the policy while Mongo is gone
(no failed or empty tools/list), the change stream comes back on its own (resumed with the
stored token), and a policy edit made after the restart reaches the gateway within a few
seconds without any restart of the gateway.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pymongo import MongoClient, WriteConcern

from mcp_gateway.settings import StaticUpstream
from tests.conftest import port_open
from tests.drills.support import (
    CONTAINER,
    GUEST_FLOOR,
    REPO,
    drill_settings,
    list_names,
    running_gateway,
    wait_until,
)

pytestmark = pytest.mark.drill

NAME = "mcp-drill-mongo"
PORT = 27018
URI = f"mongodb://127.0.0.1:{PORT}/?replicaSet=rs0&directConnection=true"
SETUP_URI = f"mongodb://127.0.0.1:{PORT}/?directConnection=true"  # works before rs.initiate
DB = "mcp_gateway"
MAJORITY = WriteConcern(w="majority", wtimeout=10_000)
PRIVATE = "sample-private_secret_lookup"


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([CONTAINER, *args], capture_output=True, text=True, check=check)


def primary(client: MongoClient) -> bool:
    return client.admin.command("hello").get("isWritablePrimary") is True


@pytest.fixture(scope="module")
def drill_mongo() -> Iterator[MongoClient]:
    if shutil.which(CONTAINER) is None:
        pytest.skip(f"container runtime {CONTAINER!r} not found")
    sh("rm", "-f", NAME, check=False)
    sh(
        "run", "-d", "--name", NAME, "-p", f"{PORT}:27017",
        "docker.io/library/mongo:8", "--replSet", "rs0", "--bind_ip_all",
    )  # fmt: skip
    client: MongoClient = MongoClient(SETUP_URI, serverSelectionTimeoutMS=2000)
    try:
        wait_until(lambda: client.admin.command("ping")["ok"] == 1, 60, 0.5, what="drill mongo up")
        client.admin.command(
            "replSetInitiate", {"_id": "rs0", "members": [{"_id": 0, "host": "localhost:27017"}]}
        )
        wait_until(lambda: primary(client), 60, 0.5, what="drill mongo primary")
        db = client.get_database(DB, write_concern=MAJORITY)
        now = datetime.now(UTC)
        for coll in ("upstreams", "toolsets", "grants"):
            docs = json.loads((REPO / "local" / "seed" / f"{coll}.json").read_text())
            if docs:
                db[coll].insert_many(docs)
        db.meta.insert_one({"_id": "policy", "version": 1, "updatedAt": now, "updatedBy": "drill"})
        yield client
    finally:
        client.close()
        sh("rm", "-f", NAME, check=False)


def bump_toolset(client: MongoClient, allow: list[str]) -> int:
    db = client.get_database(DB, write_concern=MAJORITY)
    db.toolsets.update_one({"_id": "public-basics"}, {"$set": {"allow": allow}})
    return db.meta.find_one_and_update(
        {"_id": "policy"}, {"$inc": {"version": 1}}, return_document=True
    )["version"]


def test_mongo_restart_policy_stays_served_and_watcher_resumes(drill_mongo: MongoClient) -> None:
    if not (port_open("127.0.0.1", 8081) and port_open("127.0.0.1", 8082)):
        pytest.skip("sample upstreams on :8081/:8082 not running (make up)")
    settings = drill_settings(
        mongo_uri=URI,
        policy_source="mongo",
        upstreams_source="static",  # the drill is about the policy store
        static_upstreams=[
            StaticUpstream(
                id="sample-public", url="http://localhost:8081/mcp", protocol_mode="2026-07-28"
            ),
            StaticUpstream(
                id="sample-private", url="http://localhost:8082/mcp", protocol_mode="2026-07-28"
            ),
        ],
        policy_poll_sec=2,
    )
    with running_gateway(settings) as (base, gw):
        assert list_names(base) == GUEST_FLOOR
        store = gw.policy_store
        wait_until(lambda: store.stream_up, 10, what="change stream open")
        resumes0, reopens0 = store.resumes, store.full_reopens

        outcomes: Counter[str] = Counter()
        stop = threading.Event()

        def guest_traffic() -> None:
            while not stop.is_set():
                try:
                    names = list_names(base, timeout=5)
                    outcomes["ok" if names == GUEST_FLOOR else f"wrong:{sorted(names)}"] += 1
                except Exception as exc:
                    outcomes[f"error:{type(exc).__name__}"] += 1
                time.sleep(0.05)

        threads = [threading.Thread(target=guest_traffic) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            t0 = time.monotonic()
            sh("restart", "--time", "5", NAME)  # SIGTERM -> clean step-down and restart
            wait_until(lambda: primary(drill_mongo), 60, 0.5, what="primary after restart")
            back = time.monotonic() - t0
            took = wait_until(
                lambda: (
                    store.stream_up and (store.resumes + store.full_reopens) > (resumes0 + reopens0)
                ),
                60,
                0.2,
                what="change stream reopened",
            )
            time.sleep(1)
        finally:
            stop.set()
            for t in threads:
                t.join(10)
        assert set(outcomes) == {"ok"}, outcomes  # policy served throughout
        resumed = store.resumes > resumes0
        print(
            f"\n  mongo restart: primary back after {back:.1f}s, stream reopened "
            f"{took:.1f}s later ({'resumed with token' if resumed else 'full reopen'}); "
            f"{outcomes['ok']} guest lists during the drill, all correct"
        )

        # The watcher is live again: an edit after the restart reaches the gateway.
        original = sorted(GUEST_FLOOR)
        version = bump_toolset(drill_mongo, [*original, PRIVATE])
        took = wait_until(lambda: PRIVATE in list_names(base), 10, 0.1, what="edit after restart")
        assert gw.snapshot.version == version
        bump_toolset(drill_mongo, original)
        wait_until(lambda: list_names(base) == GUEST_FLOOR, 10, 0.1, what="revert after restart")
        print(f"  edit after restart visible in {took * 1000:.0f} ms")
        assert gw.watcher is not None and gw.watcher.staleness_seconds() < 10
