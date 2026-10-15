from __future__ import annotations

from starlette.testclient import TestClient

from mcp_gateway.app import build_gateway
from mcp_gateway.policy.middleware import PolicyMiddleware
from mcp_gateway.settings import Settings


def test_app_routes_and_middleware_order(settings: Settings) -> None:
    gw = build_gateway(settings)
    names = [
        type(m).__name__
        for m in gw.mcp.middleware
        if type(m).__name__ != "DereferenceRefsMiddleware"
    ]
    assert names == [
        "PolicyMiddleware",
        "AuditMiddleware",
        "MetricsMiddleware",
        "ErrorHandlingMiddleware",
    ]
    assert isinstance(gw.mcp.middleware[1], PolicyMiddleware) or isinstance(
        gw.mcp.middleware[0], PolicyMiddleware
    )
    assert gw.registry.namespaces == ("public", "private")
    assert gw.registry.namespace_of("public_echo") == "public"
    assert gw.registry.namespace_of("nope") == "unknown"

    with TestClient(gw.app) as c:  # runs the lifespan; proxies are lazy so no upstream needed
        assert c.get("/healthz").json() == {"status": "ok"}
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["started"] is True
        m = c.get("/metrics")
        assert m.status_code == 200 and "gateway_build_info" in m.text
        w = c.get("/.well-known/oauth-protected-resource").json()
        assert w["resource"] == "http://localhost:8080/mcp"
        assert c.get("/mcp").status_code == 405  # stateless: no GET stream
    assert c.get("/readyz").status_code == 503  # after shutdown


def test_readyz_503_before_lifespan(settings: Settings) -> None:
    gw = build_gateway(settings)
    c = TestClient(gw.app)  # no context manager: lifespan never runs
    assert c.get("/readyz").status_code == 503
    assert c.get("/healthz").status_code == 200
