from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from starlette.testclient import TestClient

from mcp_gateway.app import build_gateway
from mcp_gateway.policy.snapshot import PolicySnapshot
from mcp_gateway.settings import Settings

H = {"X-Internal-Token": "s3cret"}


def test_internal_routes_404_when_token_unset(settings: Settings) -> None:
    gw = build_gateway(settings)
    with TestClient(gw.app) as c:
        assert c.get("/internal/catalog").status_code == 404
        assert c.get("/internal/upstreams/health", headers=H).status_code == 404
        assert c.post("/internal/upstreams/reload", headers=H).status_code == 404
        assert c.get("/internal/effective-tools", headers=H).status_code == 404


def test_internal_routes_403_without_matching_token(settings: Settings) -> None:
    gw = build_gateway(settings.model_copy(update={"internal_token": "s3cret"}))
    with TestClient(gw.app) as c:
        assert c.get("/internal/catalog").status_code == 403
        assert c.get("/internal/catalog", headers={"X-Internal-Token": "s3cre"}).status_code == 403
        assert (
            c.get("/internal/catalog", headers={"X-Internal-Token": "s3cret "}).status_code == 403
        )
        assert c.get("/internal/upstreams/health", headers=H).status_code == 200


def test_health_catalog_and_effective_tools_shapes(settings: Settings) -> None:
    gw = build_gateway(settings.model_copy(update={"internal_token": "s3cret"}))
    with TestClient(gw.app) as c:
        health = c.get("/internal/upstreams/health", headers=H).json()
        assert set(health["upstreams"]) == {"public", "private"}
        assert health["upstreams"]["public"]["mounted"] is True
        assert health["probe_interval_sec"] == 30.0

        cat = c.get("/internal/catalog", headers=H).json()  # upstreams may be down: still 200
        assert sorted(cat["upstreams"]) == ["private", "public"]
        assert isinstance(cat["tools"], list)

        eff = c.get("/internal/effective-tools", headers=H).json()
        assert eff["principal"]["principal_kind"] == "anonymous"
        assert eff["grants"] == ["*allow-all*"] and isinstance(eff["tools"], list)

        claims = (
            base64.urlsafe_b64encode(
                json.dumps({"sub": "u1", "azp": "ci", "scope": "a b"}).encode()
            )
            .decode()
            .rstrip("=")
        )
        eff = c.get(f"/internal/effective-tools?claims={claims}", headers=H).json()
        assert eff["principal"] == {"principal_kind": "user", "sub": "u1", "client_id": "ci"}

        assert c.get("/internal/effective-tools?kind=user", headers=H).status_code == 400
        assert c.get("/internal/effective-tools?claims=%%%", headers=H).status_code == 400
        assert c.get("/internal/effective-tools?kind=robot", headers=H).status_code == 400

        # A snapshot type that has not implemented effective_tools yet (WP3 in progress).
        gw.set_snapshot(PolicySnapshot(version=1, loaded_at=datetime.now(UTC)))
        r = c.get("/internal/effective-tools", headers=H)
        assert r.status_code == 501 and "PolicySnapshot" in r.json()["error"]

        # Reload works for the static source and is a no-op.
        r = c.post("/internal/upstreams/reload", headers=H)
        assert r.status_code == 200 and set(r.json()["upstreams"]) == {"public", "private"}


def test_internal_routes_never_go_through_mcp(settings: Settings) -> None:
    gw = build_gateway(settings.model_copy(update={"internal_token": "s3cret"}))
    with TestClient(gw.app) as c:
        # Wrong method / unknown path under /internal never reach the FastMCP mount.
        r = c.post("/internal/catalog", headers=H)
        assert r.status_code in (404, 405) and "jsonrpc" not in r.text
        r = c.post("/internal/whatever", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 404 and r.json() == {"error": "not found"}
