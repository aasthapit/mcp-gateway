# FastMCP (Python) — state as of 2026-10-14 (research notes for the gateway)

Current major is **FastMCP 4**. 3.0 went GA 2026-02-18; **4.0.0 shipped 2026-08-31**; **4.1.0 is the latest stable (2026-10-08)**. 3.x is a maintenance line (3.4.8, security backports only). Repo: `github.com/PrefectHQ/fastmcp` (jlowin URL redirects); source under `fastmcp_slim/fastmcp/`. Sources: GitHub releases, PyPI (4.1.0, Python ≥3.10). Items marked **[UNVERIFIED]** were not confirmed against primary sources.

## 1. Versions and what changed in 3.0 / 4.0

**3.0** (https://gofastmcp.com/getting-started/upgrading/from-fastmcp-2.md)
- Providers/transforms architecture (`FastMCP(providers=[...], transforms=[...])`, `add_provider`, `add_transform`). Mount namespacing, tag filtering, conflict resolution moved onto this.
- `include_tags`/`exclude_tags` constructor kwargs removed → `server.enable(tags=..., only=True)` / `server.disable(tags=...)`.
- `tool.enable()/disable()` removed → `server.disable(names={"my_tool"}, components={"tool"})`.
- `get_tools()` dict → `list_tools()` list. `ctx.set_state/get_state` async. Transport kwargs (`host`, `port`, `stateless_http`, `json_response`) moved from `FastMCP()` to `run()`/`http_app()`.
- `FASTMCP_SERVER_AUTH_*` env autoload removed.
- Deprecated in 3, removed in 4: `FastMCP.as_proxy()` → `create_proxy()`; `mount(prefix=)` → `mount(namespace=)`; `import_server()` → `mount()`; `mount(as_proxy=True)` → mount a `create_proxy(sub)`; `fastmcp.server.proxy` → `fastmcp.server.providers.proxy`; `add_tool_transformation` → `add_transform(ToolTransform(...))`.

**4.0** (https://gofastmcp.com/getting-started/whats-new.md, https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3.md, https://github.com/jlowin/fastmcp/releases/tag/v4.0.0)
- Built on **MCP Python SDK v2**; supports the **`2026-07-28` modern/sessionless protocol era** and the legacy handshake era, negotiated per connection.
- Removed from server: `ctx.sample()`, `ctx.list_roots()`, `FastMCP(sampling_handler=)`. `ctx.elicit()` only on legacy-era connections. Replacement: `InputRequiredResult` multi-round guard tool.
- Middleware `on_message/on_request/on_notification` see every inbound message incl. notifications and malformed requests; `on_initialize` never runs on the modern era.
- `ctx.set_state` does not persist across calls on the modern era; use `UserSession`/`SessionId`.
- Proxies no longer validate backend results or mutate shared transports; mirror the front's protocol era to the backend.
- Auth: `require_roles`, `InsufficientScopeError` step-up, `ClientCredentialsOAuthProvider`, identity assertion (SEP-990, beta). `AuthCheck, AuthContext, require_scopes, require_roles` → `fastmcp.server.auth`.
- httpx → **httpx2**. `McpError(code=, message=)` keyword-only. Resource-not-found is `-32602`.
- 4.1.0: HTTP sessions idle-expire after 30 min; tool lookups respect transforms, enabled state, and auth (4.0.11); protocol extensions.

## 2. Proxying (https://gofastmcp.com/servers/proxy, source `providers/proxy.py`)

```python
from fastmcp.server import create_proxy
proxy = create_proxy("http://example.com/mcp", name="MyProxy")   # URL, Path, transport, Client, config dict
```
`create_proxy(target, *, mode: str | None = None, **settings) -> FastMCPProxy`; `FastMCPProxy(*, client_factory, provider_error_strategy="warn", identity="proxy", **kwargs)`; `ProxyProvider(client_factory, cache_ttl=None)`; `ProxyClient(transport, *, forward_incoming_headers=True, **kwargs)`.

- **1:n**: `{"mcpServers": {...}}` config dict → composite proxy with automatic prefixing (`weather_get_forecast`). Or `server.mount(create_proxy(url), namespace="api")`.
- **Prefixing** (https://gofastmcp.com/servers/transforms/namespace.md): tools/prompts `{prefix}_{name}`; resources `proto://{prefix}/path`. `mount(..., tool_names={orig: custom})` overrides names.
- **Sessions**: default is a fresh backend session per request via `client_factory`; passing a connected `Client` shares one session (docs warn about context mixing). `FastMCPProxy(client_factory=lambda: shared_client)` allowed "only for stateless HTTP backends". `StatefulProxyClient` caches one backend client per front connection.
- **Transports**: stdio (`Path`, `npx`/`uvx` config), streamable-HTTP, SSE. Inbound `Authorization` and custom headers are **forwarded to HTTP/SSE upstreams by default**; `forward_incoming_headers=False` disables.
- **Upstream down**: proxy creation is lazy. Transport failures → `MCPError(code=INTERNAL_ERROR, "Client failed to connect: ...")`. In aggregate ops, `provider_error_strategy="warn"` (default) logs and skips; `"raise"` surfaces.
- **Caching** (3.2.0): `ProxyProvider` caches component lists, TTL 300 s default, shared across sessions; `cache_ttl=0` disables. ~300–400 ms per proxied `list_tools` vs 1–2 ms local.
- **Protocol era** (4.0): backend mirrors the front's era unless `mode=` pins it. Results relayed unchanged. `ProxyMetadataMiddleware` forwards upstream `instructions`; `identity="upstream"` exposes upstream `serverInfo`.
- Mirrored components can't be modified; `.copy()` then `add_tool`.

## 3. Tool filtering / visibility

**Middleware** (https://gofastmcp.com/servers/middleware): hooks `on_message → on_request → on_call_tool/on_list_tools/on_read_resource/...`, `on_initialize` (legacy only), `on_discover` (4.x). `MiddlewareContext`: `method, source, type, message, timestamp, fastmcp_context`. `on_list_tools` returns `list[Tool]` (FastMCP objects).

```python
class PrivateToolMiddleware(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if "private" not in tool.tags]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if context.fastmcp_context:
            tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
            if "private" in tool.tags:
                raise ToolError("Tool not found")
        return await call_next(context)
```
List filtering does **not** block calls — also block in `on_call_tool`. Request-scoped facts: `await context.fastmcp_context.set_state(...)`.

**Caller identity** (https://gofastmcp.com/servers/dependency-injection.md): `get_http_request()` (Starlette `Request`), `get_http_headers()` (never raises; **strips `authorization` and `cookie` by default** — `get_http_headers(include={"authorization"})` or `include_all=True`), `get_access_token() -> AccessToken | None` (`.client_id, .scopes, .expires_at, .claims`). **[UNVERIFIED]** whether `get_access_token()` works inside middleware (ResponseCachingMiddleware partitions by token, implying yes).

**Global filtering** (https://gofastmcp.com/servers/visibility.md): `mcp.enable()/disable(names=, tags=, keys=, version=, components=)`; `enable(tags={"safe"}, only=True)` allowlist. Global, all clients.

**Per-session**: `ctx.enable_components(tags=...)` / `disable_components` / `reset_visibility` — session state, triggers `list_changed` for that session. Source `get_session_transforms` raises without `session_id`; **[UNVERIFIED]** on the modern sessionless era.

**Transforms** (https://gofastmcp.com/servers/transforms): provider/server-level pipeline (`list_tools(tools)` / `get_tool(name, call_next)`). Static/global.

**Per-caller authorization** (https://gofastmcp.com/servers/authorization.md): `@mcp.tool(auth=require_scopes("read"))`, `require_roles("admin", extract=fn)`, `restrict_tag`, or any `Callable[[AuthContext], bool]` reading `ctx.token.claims` / `ctx.component`. Server-wide: `AuthMiddleware(auth=...)`. Requires an `AuthProvider` (token present).

Verdict: a plain `Middleware` with `on_list_tools` + `on_call_tool` works without an `AuthProvider` and is the route for optional auth.

## 4. Authentication (https://gofastmcp.com/servers/auth/authentication, .../token-verification, .../remote-oauth.md, .../multi-auth.md)

Providers: `TokenVerifier` base (`verify_token(token) -> AccessToken | None`), `JWTVerifier`, `StaticTokenVerifier` (dev only), `IntrospectionTokenVerifier` (RFC 7662), `DebugTokenVerifier`, `RemoteAuthProvider` (verifier + `/.well-known/oauth-protected-resource`), `OAuthProxy`, `OIDCProxy`, `OAuthProvider` (full AS), `MultiAuth` (3.1), `ClientCredentialsOAuthProvider` (4.0). `AuthProvider` methods: `verify_token, get_routes, get_well_known_routes, get_middleware, scopes_supported, challenge_scopes`.

```python
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

token_verifier = JWTVerifier(
    jwks_uri="https://auth.yourcompany.com/.well-known/jwks.json",
    issuer="https://auth.yourcompany.com",
    audience="mcp-production-api",
    required_scopes=["read:data"],      # also: algorithm="Ed25519", jwks_refresh_interval
)
auth = RemoteAuthProvider(
    token_verifier=token_verifier,
    authorization_servers=[AnyHttpUrl("https://auth.yourcompany.com")],
    base_url="https://api.yourcompany.com",
)
mcp = FastMCP(name="Company API", auth=auth)
```
JWKS keys cached 1 h; on streamable-HTTP a session is bound to the `(client_id, issuer, subject)` that created it.

**Optional auth is NOT supported.** Enforcement is ASGI `RequireAuthMiddleware` around the MCP route (source `http.py`); no `Bearer` → 401 + `WWW-Authenticate: Bearer resource_metadata="..."` before any MCP method. Feature request https://github.com/PrefectHQ/fastmcp/issues/3291 closed wontfix (Feb 2026); suggested workaround: two servers merged by a gateway.

Recommended custom approach: `auth=None`, verify in our own middleware using `JWTVerifier.verify_token(token)` (public, returns `AccessToken | None`); `get_access_token()` will be `None`, so `require_scopes` etc. won't work — do authorization in own middleware. `AuthProvider.get_well_known_routes(mcp_path=...)` can be mounted as plain Starlette routes **[UNVERIFIED that this is intended without `auth=`]**. `custom_route`s bypass auth even with `auth=`.

## 5. Deployment / scaling (https://gofastmcp.com/deployment/http)

```python
app = mcp.http_app(path="/mcp", stateless_http=True)   # json_response=, event_store=, retry_interval=, middleware=[...], host_origin_protection=, session_idle_timeout=
# uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4   (multi-worker requires stateless mode)
# FASTMCP_STATELESS_HTTP=true / FASTMCP_JSON_RESPONSE=true
```
- Legacy-era streamable HTTP keeps sessions in memory per instance; sticky sessions "don't work reliably" → `stateless_http=True`. In stateless mode the route accepts only `POST, DELETE` (no GET stream): `tools/list_changed`, server-initiated elicitation/sampling, session-scoped visibility unavailable. `report_progress` within a request's own stream **[UNVERIFIED]**.
- Modern era: any replica can serve any request. Persistent state → `UserSession`/`SessionId` with a shared `session_state_store` (Redis via `key_value`).
- Mounting: `Starlette(routes=[Mount("/mcp-server", app=mcp_app)], lifespan=mcp_app.lifespan)` — lifespan required. FastAPI: `mcp.http_app(path="/")` + `api.mount("/mcp", mcp_app)`.
- Custom routes: `@mcp.custom_route("/health", methods=["GET"])`; bypass auth; lowest precedence.
- 4.0 routing headers from modern clients: `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*`.
- `host_origin_protection=True, allowed_hosts=[...]` for DNS rebinding; reverse proxies need `proxy_buffering off` and long timeouts.

## 6. Observability (https://gofastmcp.com/servers/telemetry)

Native OpenTelemetry tracing (no-op without SDK): SERVER span per request (`tools/call {name}` etc.); attributes `mcp.method.name, mcp.session.id, mcp.protocol.version, gen_ai.tool.name, enduser.id, enduser.scope, fastmcp.provider.type, fastmcp.proxy.backend_name, error.type`; `delegate {name}` spans; trace context propagates through proxies. `FASTMCP_TELEMETRY_MODE=native|propagation_only|off`. **No built-in metrics.** Built-in middleware: `LoggingMiddleware`/`StructuredLoggingMiddleware`, `TimingMiddleware`/`DetailedTimingMiddleware`, `RateLimitingMiddleware`, `ErrorHandlingMiddleware(error_callback=)`, `ResponseCachingMiddleware`, `ResponseLimitingMiddleware`.

## 7. Gotchas

- Name collisions (https://gofastmcp.com/servers/composition): un-namespaced mounts → highest version wins, else first-mounted; always namespace.
- Schema passthrough: proxied tools mirror upstream `inputSchema`/`outputSchema`; results unvalidated. Resource templates path-screened (`ResourceSecurity`).
- Cache staleness: 300 s TTL on proxied lists.
- Lifespans: mounted servers' lifespans run with the parent; proxies lazy; `ProxyProvider` has no documented pool.
- Timeouts: not documented for proxies **[UNVERIFIED]**; `StreamableHttpTransport(sse_read_timeout=)` removed in 4.0; configure via `httpx_client_factory`.
- `get_http_headers()` hides `authorization`; proxies forward it upstream by default. `ctx.session_id` raises without a session.
- Tasks (`task=True`) cannot traverse a proxy.

## Related gateways

- **Prefect Horizon** (https://gofastmcp.com/deployment/prefect-horizon.md): commercial hosted gateway with tool-level RBAC; not open source.
- **IBM ContextForge** (https://github.com/IBM/mcp-context-forge): Apache-2.0, FastAPI/SQLAlchemy, not FastMCP; federates upstreams; virtual servers with `associated_tools`; RBAC/teams.
- **Docker MCP Gateway** (https://github.com/docker/mcp-gateway): MIT, Go; profiles with per-profile tool enable/disable. Others: agentgateway, MetaMCP, Unla, Bifrost.
