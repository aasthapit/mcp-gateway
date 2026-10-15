"""Drill: rolling restart of two gateway processes behind a round-robin balancer.

Simulates a Deployment rollout with ``maxSurge: 1, maxUnavailable: 0``: for each old pod a
new gateway process is started and must pass ``/readyz`` before the old one is taken out
of rotation (Endpoints removal), given the preStop pause, sent SIGTERM and awaited. The
balancer (tests/drills/support.py RoundRobinProxy) plays the OpenShift router: it routes
only to ready, non-terminating backends and retries on connection failures. Meanwhile
guest clients run a tools/list + tools/call mix through it.

Expected (PLAN WP7 done-when): zero HTTP 5xx and zero transport errors for the guest
clients, the old processes drain and stop within the grace period, the new ones serve
traffic. The ``sigterm-first`` variant sends SIGTERM without the Endpoints/preStop grace
(the balancer only learns from /readyz turning 503 and from refused connections), which
must still be clean.
"""

from __future__ import annotations

import signal
import threading
import time
from collections import Counter

import httpx
import pytest

from tests.conftest import free_port, port_open
from tests.drills.support import (
    GUEST_FLOOR,
    Backend,
    GatewayProcess,
    RoundRobinProxy,
    call_ok,
    mcp_post,
    rpc_message,
    serve_in_thread,
    wait_until,
)

pytestmark = pytest.mark.drill

PRESTOP_SEC = 1.0
GRACE_SEC = 10


def gateway_env() -> dict[str, str]:
    return {
        "GATEWAY_ENV": "dev",
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_METRICS_PORT": "0",
        "GATEWAY_LOG_LEVEL": "INFO",
        "GATEWAY_LOG_JSON": "true",
        "GATEWAY_RATE_LIMIT_ENABLED": "false",  # every drill client is the same guest IP
        "GATEWAY_SHUTDOWN_GRACE_SEC": str(GRACE_SEC),
        "GATEWAY_UPSTREAM_PROBE_SEC": "5",
    }


@pytest.mark.parametrize("order", ["endpoints-first", "sigterm-first"])
def test_rolling_restart_zero_5xx_for_guests(tmp_path, order: str) -> None:
    if not (port_open("127.0.0.1", 8081) and port_open("127.0.0.1", 27017)):
        pytest.skip("local stack not running (make up && make seed)")
    procs = [GatewayProcess(free_port(), gateway_env(), tmp_path / f"gw-{i}.log") for i in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.wait_ready()
    backends = [Backend(p.base) for p in procs]
    proxy = RoundRobinProxy(backends)
    lb = serve_in_thread(proxy.app, proxy.port)
    wait_until(lambda: all(b.ready for b in backends), 10, what="balancer sees both backends")

    outcomes: Counter[str] = Counter()
    stop = threading.Event()

    def guest(i: int) -> None:
        with httpx.Client(timeout=30) as client:
            n = 0
            while not stop.is_set():
                n += 1
                try:
                    if n % 2:
                        r = mcp_post(lb.base, "tools/list", client=client)
                        names = (
                            {t["name"] for t in rpc_message(r)["result"]["tools"]}
                            if r.status_code == 200
                            else set()
                        )
                        good = r.status_code == 200 and names >= GUEST_FLOOR
                    else:
                        r = mcp_post(
                            lb.base,
                            "tools/call",
                            {"name": "sample-public_echo", "arguments": {"text": f"g{i}-{n}"}},
                            client=client,
                        )
                        good = r.status_code == 200 and call_ok(rpc_message(r))
                    outcomes["ok" if good else f"http_{r.status_code}"] += 1
                except Exception as exc:
                    outcomes[f"error:{type(exc).__name__}"] += 1

    threads = [threading.Thread(target=guest, args=(i,), daemon=True) for i in range(4)]
    new_procs: list[GatewayProcess] = []
    exit_codes: list[int] = []
    try:
        for t in threads:
            t.start()
        time.sleep(2)  # steady state first
        t0 = time.monotonic()
        for old, old_backend in zip(procs, list(backends), strict=True):
            new = GatewayProcess(
                free_port(), gateway_env(), tmp_path / f"gw-new-{len(new_procs)}.log"
            )
            new.start()
            new_procs.append(new)
            new_backend = Backend(new.base, ready=False)
            backends.append(new_backend)  # the balancer routes to it once /readyz is 200
            new.wait_ready()
            wait_until(lambda nb=new_backend: nb.ready, 5, what="new backend in rotation")
            if order == "endpoints-first":
                old_backend.in_rotation = False  # pod deleted: Endpoints drop it ...
                time.sleep(PRESTOP_SEC)  # ... while preStop sleeps, then SIGTERM
            exit_codes.append(old.terminate(timeout=GRACE_SEC + 15))
            old_backend.in_rotation = False
        rollout = time.monotonic() - t0
        time.sleep(2)  # steady state after
    finally:
        stop.set()
        for t in threads:
            t.join(35)
        lb.stop()
        for p in procs + new_procs:
            if p.proc is not None and p.proc.poll() is None:
                p.proc.kill()

    total = sum(outcomes.values())
    served_new = sum(b.served for b in backends[2:])
    print(
        f"\n  rolling restart ({order}) in {rollout:.1f}s: {total} guest requests, outcomes {dict(outcomes)}, "
        f"balancer retries {proxy.retries}, no-backend {proxy.no_backend}, "
        f"new pods served {served_new}, old exit codes {exit_codes}"
    )
    assert set(outcomes) == {"ok"}, outcomes  # zero 5xx, zero transport errors
    assert proxy.no_backend == 0
    # uvicorn re-raises the captured SIGTERM after its graceful shutdown, so the exit status
    # is "terminated by SIGTERM" (143 in a container); the logs prove the clean path.
    assert exit_codes == [-signal.SIGTERM, -signal.SIGTERM]
    for old in procs:
        log = old.log_path.read_text()
        assert "draining: readiness off" in log and "gateway stopped" in log, old.log_path
        assert "Cancel " not in log  # nothing was cut by the grace timeout
    assert all(b.served > 0 for b in backends[2:])
    assert total > 100
