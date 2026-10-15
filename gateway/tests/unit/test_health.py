from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_gateway.health import Readiness, healthz, make_readyz


def test_readiness_flow() -> None:
    r = Readiness()
    app = Starlette(routes=[Route("/healthz", healthz), Route("/readyz", make_readyz(r))])
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/readyz").status_code == 503
        r.started = True
        assert c.get("/readyz").status_code == 200

        r.register("mongo", lambda: (False, {"error": "down"}))
        body = c.get("/readyz")
        assert body.status_code == 503
        assert body.json()["checks"]["mongo"] == {"ok": False, "critical": True, "error": "down"}

        r.register("mongo", lambda: (True, {}))

        async def stale() -> tuple[bool, dict]:
            return False, {"age": 999}

        r.register("policy_fresh", stale, critical=False)
        body = c.get("/readyz")
        assert body.status_code == 200
        assert body.json()["checks"]["policy_fresh"]["critical"] is False

        def boom() -> tuple[bool, dict]:
            raise RuntimeError("x")

        r.register("boom", boom)
        body = c.get("/readyz")
        assert body.status_code == 503
        assert "RuntimeError" in body.json()["checks"]["boom"]["error"]
