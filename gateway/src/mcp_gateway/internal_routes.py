"""``/internal/*``: admin-facing routes on the outer Starlette app, never through ``/mcp``.

Protected by a shared secret: ``X-Internal-Token`` must equal ``GATEWAY_INTERNAL_TOKEN``
(constant-time compare). When the token is not configured every route answers 404 so the
surface does not exist. In OpenShift a NetworkPolicy additionally limits callers to the
admin-api pod (PLAN section 3.5).

* ``GET  /internal/catalog``          every tool across mounted upstreams (admin UI picker)
* ``GET  /internal/upstreams/health`` last probe result per upstream
* ``POST /internal/upstreams/reload`` re-read the upstream list and reconcile (until WP3)
* ``GET  /internal/effective-tools``  tools a principal would see; ``?kind=anonymous`` or
  ``?claims=<base64url JSON>``. 501 until the snapshot type implements ``effective_tools``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_gateway.auth.principal import ANONYMOUS, Principal
from mcp_gateway.telemetry import get_logger

if TYPE_CHECKING:
    from mcp_gateway.app import Gateway

log = get_logger(__name__)

TOKEN_HEADER = "X-Internal-Token"
Handler = Callable[[Request], Awaitable[Response]]


def _authorize(request: Request, token: str | None) -> Response | None:
    if not token:
        return JSONResponse({"error": "not found"}, status_code=404)
    presented = request.headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(presented.encode(), token.encode()):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


def _protected(gateway: Gateway, handler: Handler) -> Handler:
    async def wrapped(request: Request) -> Response:
        denied = _authorize(request, gateway.settings.internal_token)
        if denied is not None:
            return denied
        return await handler(request)

    return wrapped


def principal_from_query(request: Request) -> Principal:
    """``kind=anonymous`` (default) or ``claims=<base64url JSON>`` describing a user."""
    claims_b64 = request.query_params.get("claims")
    kind = request.query_params.get("kind", "user" if claims_b64 else "anonymous")
    if kind == "anonymous":
        return ANONYMOUS
    if kind != "user":
        raise ValueError(f"kind must be anonymous or user, got {kind!r}")
    if not claims_b64:
        raise ValueError("kind=user requires claims=<base64url JSON>")
    try:
        padded = claims_b64 + "=" * (-len(claims_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"claims is not base64url JSON: {exc}") from exc
    if not isinstance(claims, dict):
        raise ValueError("claims must decode to a JSON object")
    scope = claims.get("scope", "")
    scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
    return Principal(
        kind="user",
        sub=claims.get("sub"),
        client_id=claims.get("azp") or claims.get("client_id"),
        scopes=scopes,
        claims=claims,
    )


def build_internal_routes(gateway: Gateway) -> list[Route]:
    async def catalog(request: Request) -> Response:
        tools = await gateway.registry.catalog()
        return JSONResponse(
            {
                "upstreams": sorted(gateway.registry.namespaces),
                "tools": [t.as_dict() for t in tools],
            }
        )

    async def upstreams_health(request: Request) -> Response:
        health = gateway.registry.health()
        return JSONResponse(
            {
                "upstreams": {uid: h.as_dict() for uid, h in sorted(health.items())},
                "probe_interval_sec": gateway.settings.upstream_probe_sec,
            }
        )

    async def upstreams_reload(request: Request) -> Response:
        try:
            health = await gateway.reload_upstreams()
        except Exception as exc:
            return JSONResponse(
                {"error": f"reload failed: {type(exc).__name__}: {exc}"}, status_code=502
            )
        return JSONResponse({"upstreams": {uid: h.as_dict() for uid, h in sorted(health.items())}})

    async def effective_tools(request: Request) -> Response:
        try:
            principal = principal_from_query(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        snapshot = gateway.snapshot
        try:
            allowed = snapshot.effective_tools(principal)
        except NotImplementedError:
            return JSONResponse(
                {"error": f"{type(snapshot).__name__} does not implement effective_tools (WP3)"},
                status_code=501,
            )
        tools = await gateway.registry.catalog()
        body: dict[str, Any] = {
            "principal": principal.audit_fields(),
            "policy_version": snapshot.version,
            "tools": sorted(t.namespaced_name for t in tools if allowed.allows(t.namespaced_name)),
        }
        try:
            body["grants"] = snapshot.matching_grants(principal)
        except NotImplementedError:
            body["grants"] = None
        return JSONResponse(body)

    async def not_found(request: Request) -> Response:
        return JSONResponse({"error": "not found"}, status_code=404)

    return [
        Route("/internal/catalog", _protected(gateway, catalog), methods=["GET"]),
        Route("/internal/upstreams/health", _protected(gateway, upstreams_health), methods=["GET"]),
        Route(
            "/internal/upstreams/reload", _protected(gateway, upstreams_reload), methods=["POST"]
        ),
        Route("/internal/effective-tools", _protected(gateway, effective_tools), methods=["GET"]),
        # Anything else under /internal/ stops here instead of falling through to the
        # FastMCP mount (any method).
        Route(
            "/internal/{rest:path}",
            not_found,
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        ),
    ]
