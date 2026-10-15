"""Assembles the gateway: FastMCP server, middleware chain, proxies, Starlette app.

Layout of the ASGI app::

    Starlette (outer)
      /healthz, /readyz, /metrics, /.well-known/oauth-protected-resource[/mcp]  (plain routes)
      /healthz, /readyz, /metrics, /.well-known/oauth-protected-resource   (plain routes)
      /internal/catalog, /internal/upstreams/*, /internal/effective-tools   (token-guarded)
      Mount("/")  ->  FastMCP http_app(path=/mcp, stateless_http=True)
                        HostOriginGuard -> BearerAuthMiddleware (auth/asgi.py) -> MCP transport
                          FastMCP middleware: Policy -> Audit -> Metrics -> ErrorHandling

The outer app's lifespan drives the FastMCP app's lifespan, starts the policy watcher
(initial load, then change stream + poll), mounts the upstreams (from the policy snapshot
when both come from Mongo), starts the upstream health probe and flips readiness
afterwards. The SDK ``ListenHandler`` is registered on the low-level server (ADR 0003) so
modern clients can open ``subscriptions/listen`` and receive ``tools/list_changed`` after
a policy reload.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
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
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_gateway import __version__
from mcp_gateway.auth.asgi import BearerAuthMiddleware
from mcp_gateway.auth.jwt import JwtAuth
from mcp_gateway.auth.metadata import metadata_routes
from mcp_gateway.debug_tools import DEBUG_TOOL_NAMES, register_debug_tools
from mcp_gateway.health import Readiness, healthz, make_readyz
from mcp_gateway.internal_routes import build_internal_routes
from mcp_gateway.middleware.audit import AuditMiddleware
from mcp_gateway.middleware.metrics import UPSTREAM_ERRORS, MetricsMiddleware
from mcp_gateway.policy.middleware import PolicyMiddleware
from mcp_gateway.policy.snapshot import PolicySnapshot
from mcp_gateway.policy.store import MongoPolicyStore, PolicyStore, StaticPolicyStore
from mcp_gateway.policy.watcher import PolicyHolder, PolicyWatcher
from mcp_gateway.settings import Settings, load_settings
from mcp_gateway.telemetry import configure_logging, get_logger
from mcp_gateway.upstreams.models import UpstreamHealth
from mcp_gateway.upstreams.registry import UpstreamRegistry
from mcp_gateway.upstreams.source import UpstreamSource, make_upstream_source

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
    upstream_source: UpstreamSource
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

    async def reload_upstreams(self) -> Mapping[str, UpstreamHealth]:
        """Re-read the desired upstream list and reconcile the registry.

        ``POST /internal/upstreams/reload``. When the upstreams follow the policy snapshot
        (watcher wired with ``on_reload``) this reloads the snapshot, which applies them;
        otherwise the standalone loader (static settings or the ``upstreams`` collection)
        is used, which is also the startup path for a static policy with Mongo upstreams.
        """
        if self.watcher is not None and self.watcher.on_reload is not None:
            await self.watcher.reload("manual")
            return self.registry.health()
        desired = await self.upstream_source.load()
        return await self.registry.apply_configs(desired)


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
    registry = UpstreamRegistry(mcp, settings=settings)
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
        upstream_source=make_upstream_source(settings),
        bus=bus,
        listen_handler=listen_handler,
        jwt_auth=jwt_auth,
    )
    # Ready once at least one enabled upstream is mounted; a dead upstream stays mounted and
    # does not flip readiness. Reported but non-critical when the operator opts out.
    gateway.readiness.register(
        "upstreams", registry.readiness_probe, critical=settings.ready_requires_upstream
    )
    gateway.readiness.register("oidc", jwt_auth.probe, critical=settings.is_prod)

    # Order matters: first added is outermost (FastMCP builds the chain in list order).
    # Dev-only gateway-local tools bypass the allow-list; it governs upstream tools.
    debug_tool_names = DEBUG_TOOL_NAMES if settings.env == "dev" else frozenset()
    mcp.add_middleware(
        PolicyMiddleware(snapshot_provider=gateway.policy.get, exempt=debug_tool_names)
    )
    mcp.add_middleware(AuditMiddleware(upstream_of=registry.namespace_of))
    mcp.add_middleware(MetricsMiddleware(upstream_of=registry.namespace_of))
    mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=not settings.is_prod))

    if settings.upstreams_source == "static":
        registry.mount_static(settings)  # no Mongo needed; policy reloads leave these alone
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
            # Upstreams follow the policy snapshot when both come from Mongo: the watcher
            # mounts/unmounts them on every reload. A static policy uses the standalone
            # loader below; static upstreams were mounted at build time and are left alone.
            upstreams_from_snapshot = (
                settings.upstreams_source == "mongo" and settings.policy_source == "mongo"
            )
            watcher = PolicyWatcher(
                gateway.policy_store,
                gateway.policy,
                bus=bus,
                on_reload=registry.apply if upstreams_from_snapshot else None,
                readiness=gateway.readiness,
                debounce_ms=settings.policy_debounce_ms,
            )
            gateway.watcher = watcher
            await watcher.start()  # initial load; /readyz stays 503 until it succeeds
            if settings.upstreams_source == "mongo" and not upstreams_from_snapshot:
                try:
                    await gateway.reload_upstreams()
                except Exception as exc:
                    UPSTREAM_ERRORS.labels("-", "load_failed").inc()
                    log.error(
                        "upstream list could not be loaded; no upstreams mounted",
                        source=settings.upstreams_source,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            await registry.start()
            if settings.metrics_port:
                metrics_server, _ = start_http_server(settings.metrics_port, addr=settings.host)
                log.info("metrics listener started", port=settings.metrics_port)
            gateway.readiness.started = True
            log.info(
                "gateway ready",
                version=__version__,
                env=settings.env,
                mcp_path=settings.mcp_path,
                upstreams_source=settings.upstreams_source,
                upstreams=list(registry.namespaces),
                allow_anonymous=settings.allow_anonymous,
                issuer=settings.oidc_issuer,
                policy_source=settings.policy_source,
                policy_version=gateway.snapshot.version,
                degraded={u: h.reason for u, h in registry.health().items() if not h.mounted},
            )
            try:
                yield {"gateway": gateway}
            finally:
                gateway.readiness.started = False
                listen_handler.close()  # graceful close of open listen streams
                await watcher.stop()
                await registry.stop()
                await gateway.upstream_source.close()
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
            *build_internal_routes(gateway),  # plain routes: never reach the /mcp mount
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
