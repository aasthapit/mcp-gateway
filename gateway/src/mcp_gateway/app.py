"""Assembles the gateway: FastMCP server, middleware chain, proxies, Starlette app.

Layout of the ASGI app::

    Starlette (outer)
      /healthz, /readyz, /metrics, /.well-known/oauth-protected-resource   (plain routes)
      Mount("/")  ->  FastMCP http_app(path=/mcp, stateless_http=True)
                        HostOriginGuard -> [WP2 auth ASGI middleware] -> MCP transport
                          FastMCP middleware: Policy -> Audit -> Metrics -> ErrorHandling

The outer app's lifespan drives the FastMCP app's lifespan, starts the policy watcher
(initial load, then change stream + poll) and flips readiness afterwards. The SDK
``ListenHandler`` is registered on the low-level server (ADR 0003) so modern clients can
open ``subscriptions/listen`` and receive ``tools/list_changed`` after a policy reload.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp_types import SubscriptionsListenRequestParams
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, start_http_server
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp_gateway import __version__
from mcp_gateway.health import Readiness, healthz, make_readyz
from mcp_gateway.middleware.audit import AuditMiddleware
from mcp_gateway.middleware.metrics import UPSTREAM_UP, MetricsMiddleware
from mcp_gateway.policy.middleware import PolicyMiddleware
from mcp_gateway.policy.snapshot import PolicySnapshot
from mcp_gateway.policy.store import MongoPolicyStore, PolicyStore, StaticPolicyStore
from mcp_gateway.policy.watcher import PolicyHolder, PolicyWatcher
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
    policy: PolicyHolder = field(default_factory=PolicyHolder)
    bus: InMemorySubscriptionBus = field(default_factory=InMemorySubscriptionBus)
    listen_handler: ListenHandler | None = None
    watcher: PolicyWatcher | None = None
    app: Starlette | None = None

    @property
    def snapshot(self) -> PolicySnapshot:
        """The current policy snapshot (read per request by the middleware)."""
        return self.policy.snapshot

    def set_snapshot(self, snapshot: PolicySnapshot) -> None:
        """Atomic swap; the watcher uses :meth:`PolicyWatcher.reload`, tests use this."""
        self.policy.swap(snapshot)


def policy_store_from_settings(settings: Settings) -> PolicyStore:
    if settings.policy_source == "static":
        return StaticPolicyStore()
    return MongoPolicyStore(
        settings.mongo_uri, settings.mongo_db, poll_sec=settings.policy_poll_sec
    )


def build_gateway(
    settings: Settings | None = None,
    *,
    policy_store: PolicyStore | None = None,
    asgi_middleware: list[Middleware] | None = None,
) -> Gateway:
    """Build the gateway and its Starlette app without starting anything.

    ``asgi_middleware`` is applied inside the FastMCP app (around ``/mcp`` only); WP2
    passes its auth middleware here.
    """
    settings = settings or load_settings()
    configure_logging(settings)

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
    bus = InMemorySubscriptionBus()
    listen_handler = ListenHandler(bus)
    # FastMCP 4.1 does not serve subscriptions/listen itself (ADR 0003); the SDK handler
    # works unchanged in stateless mode and advertises tools.listChanged=true.
    mcp._mcp_server.add_request_handler(
        "subscriptions/listen", SubscriptionsListenRequestParams, listen_handler
    )
    gateway = Gateway(
        settings=settings,
        mcp=mcp,
        registry=registry,
        readiness=Readiness(),
        policy_store=policy_store or policy_store_from_settings(settings),
        bus=bus,
        listen_handler=listen_handler,
    )

    # Order matters: first added is outermost (FastMCP builds the chain in list order).
    mcp.add_middleware(PolicyMiddleware(snapshot_provider=gateway.policy.get))
    mcp.add_middleware(AuditMiddleware(upstream_of=registry.namespace_of))
    mcp.add_middleware(MetricsMiddleware(upstream_of=registry.namespace_of))
    mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=not settings.is_prod))

    registry.mount_static(settings)

    mcp_app = mcp.http_app(
        path=settings.mcp_path,
        stateless_http=True,
        host_origin_protection=settings.host_origin_protection,
        allowed_hosts=settings.effective_allowed_hosts,
        middleware=asgi_middleware,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[dict[str, Any]]:
        metrics_server = None
        async with mcp_app.lifespan(mcp_app):
            watcher = PolicyWatcher(
                gateway.policy_store,
                gateway.policy,
                bus=bus,
                on_reload=registry.apply,
                readiness=gateway.readiness,
                debounce_ms=settings.policy_debounce_ms,
            )
            gateway.watcher = watcher
            await watcher.start()  # initial load; /readyz stays 503 until it succeeds
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
                policy_source=settings.policy_source,
                policy_version=gateway.snapshot.version,
            )
            try:
                yield {"gateway": gateway}
            finally:
                gateway.readiness.started = False
                listen_handler.close()  # graceful close of open listen streams
                await watcher.stop()
                if metrics_server is not None:
                    metrics_server.shutdown()

    async def metrics(request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    async def protected_resource_metadata(request: Request) -> JSONResponse:
        # RFC 9728 placeholder. WP2 fills authorization_servers/scopes from OIDC discovery
        # and serves the same document from FastMCP's auth provider routes.
        return JSONResponse(
            {
                "resource": f"{settings.public_base_url}{settings.mcp_path}",
                "authorization_servers": [],
                "bearer_methods_supported": ["header"],
                "scopes_supported": [],
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", make_readyz(gateway.readiness), methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
            Route(
                "/.well-known/oauth-protected-resource",
                protected_resource_metadata,
                methods=["GET"],
            ),
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
