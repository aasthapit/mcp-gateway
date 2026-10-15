"""Assembles the gateway: FastMCP server, middleware chain, proxies, Starlette app.

Layout of the ASGI app::

    Starlette (outer)
      /healthz, /readyz, /metrics, /.well-known/oauth-protected-resource[/mcp]  (plain routes)
      Mount("/")  ->  FastMCP http_app(path=/mcp, stateless_http=True)
                        HostOriginGuard -> BearerAuthMiddleware (auth/asgi.py) -> MCP transport
                          FastMCP middleware: Policy -> Audit -> Metrics -> ErrorHandling

The outer app's lifespan drives the FastMCP app's lifespan and flips readiness afterwards.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, start_http_server
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_gateway import __version__
from mcp_gateway.auth.asgi import BearerAuthMiddleware
from mcp_gateway.auth.jwt import JwtAuth
from mcp_gateway.auth.metadata import metadata_routes
from mcp_gateway.debug_tools import register_debug_tools
from mcp_gateway.health import Readiness, healthz, make_readyz
from mcp_gateway.middleware.audit import AuditMiddleware
from mcp_gateway.middleware.metrics import (
    POLICY_AGE,
    POLICY_VERSION,
    UPSTREAM_UP,
    MetricsMiddleware,
)
from mcp_gateway.policy.middleware import PolicyMiddleware
from mcp_gateway.policy.snapshot import AllowAllSnapshot, PolicySnapshot
from mcp_gateway.policy.store import PolicyStore, StaticPolicyStore
from mcp_gateway.settings import Settings, load_settings
from mcp_gateway.telemetry import configure_logging, get_logger
from mcp_gateway.upstreams.registry import UpstreamRegistry

log = get_logger(__name__)

INSTRUCTIONS = (
    "MCP gateway. Tools are namespaced as <upstream>_<tool>. The tool list depends on the "
    "credentials presented with each request; re-list after authenticating."
)


@dataclass
class Gateway:
    """Everything the process holds; tests and later work packages reach in through this."""

    settings: Settings
    mcp: FastMCP
    registry: UpstreamRegistry
    readiness: Readiness
    policy_store: PolicyStore
    jwt_auth: JwtAuth
    snapshot: PolicySnapshot = field(default_factory=AllowAllSnapshot)
    app: Starlette | None = None

    def set_snapshot(self, snapshot: PolicySnapshot) -> None:
        """Atomic swap used by the policy watcher (WP3)."""
        self.snapshot = snapshot
        POLICY_VERSION.set(snapshot.version)
        POLICY_AGE.set(snapshot.age_seconds)


def build_gateway(
    settings: Settings | None = None,
    *,
    policy_store: PolicyStore | None = None,
    jwt_auth: JwtAuth | None = None,
    asgi_middleware: list[Middleware] | None = None,
) -> Gateway:
    """Build the gateway and its Starlette app without starting anything.

    ``jwt_auth`` lets tests inject a verifier bound to an in-process JWKS endpoint.
    ``asgi_middleware`` is applied inside the FastMCP app (around ``/mcp`` only), after
    the bearer auth middleware.
    """
    settings = settings or load_settings()
    configure_logging(settings)
    jwt_auth = jwt_auth or JwtAuth(settings)

    mcp = FastMCP(
        "gateway",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=None,  # optional auth is implemented outside FastMCP (PLAN section 7.2)
        cache_ttl=settings.tools_list_ttl_sec,
        cache_scope="private",  # tool lists vary per caller; never share across principals
        mask_error_details=settings.is_prod,
    )
    registry = UpstreamRegistry(mcp)
    gateway = Gateway(
        settings=settings,
        mcp=mcp,
        registry=registry,
        readiness=Readiness(),
        policy_store=policy_store or StaticPolicyStore(),
        jwt_auth=jwt_auth,
    )
    gateway.readiness.register("oidc", jwt_auth.probe, critical=settings.is_prod)

    # Order matters: first added is outermost (FastMCP builds the chain in list order).
    mcp.add_middleware(PolicyMiddleware(snapshot_provider=lambda: gateway.snapshot))
    mcp.add_middleware(AuditMiddleware(upstream_of=registry.namespace_of))
    mcp.add_middleware(MetricsMiddleware(upstream_of=registry.namespace_of))
    mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=not settings.is_prod))

    registry.mount_static(settings)
    if settings.env == "dev":
        register_debug_tools(mcp)

    auth_middleware = Middleware(
        BearerAuthMiddleware,
        auth=jwt_auth,
        resource_metadata_url=settings.resource_metadata_url,
        allow_anonymous=settings.allow_anonymous,
    )
    mcp_app = mcp.http_app(
        path=settings.mcp_path,
        stateless_http=True,
        host_origin_protection=settings.host_origin_protection,
        allowed_hosts=settings.effective_allowed_hosts,
        middleware=[auth_middleware, *(asgi_middleware or [])],
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[dict[str, Any]]:
        metrics_server = None
        async with mcp_app.lifespan(mcp_app):
            await jwt_auth.startup()
            gateway.set_snapshot(await gateway.policy_store.load())
            for uid in registry.namespaces:
                UPSTREAM_UP.labels(upstream=uid).set(1)
            if settings.metrics_port:
                metrics_server, _ = start_http_server(settings.metrics_port, addr=settings.host)
                log.info("metrics listener started", port=settings.metrics_port)
            gateway.readiness.started = True
            log.info(
                "gateway ready",
                version=__version__,
                env=settings.env,
                mcp_path=settings.mcp_path,
                upstreams=list(registry.namespaces),
                allow_anonymous=settings.allow_anonymous,
                issuer=settings.oidc_issuer,
            )
            try:
                yield {"gateway": gateway}
            finally:
                gateway.readiness.started = False
                if metrics_server is not None:
                    metrics_server.shutdown()

    async def metrics(request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", make_readyz(gateway.readiness), methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
            *metadata_routes(settings),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    gateway.app = app
    return gateway


def create_app() -> Starlette:
    """ASGI factory: ``uvicorn --factory mcp_gateway.app:create_app``."""
    gateway = build_gateway()
    assert gateway.app is not None
    return gateway.app
