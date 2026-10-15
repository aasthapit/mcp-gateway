# gateway

Python 3.12 / FastMCP 4.1 MCP gateway: one stateless streamable-HTTP endpoint (`/mcp`)
that proxies upstream MCP servers as namespaced tools and filters them per caller.
Design: [docs/PLAN.md](../docs/PLAN.md) (sections 3 to 5). Verified FastMCP behaviours:
[docs/adr](../docs/adr/README.md).

**State after WP2:** the two sample upstreams are mounted statically from settings;
callers are anonymous or, with a valid `Authorization: Bearer <jwt>` from the configured
OIDC issuer, users with `sub`/`client_id`/scopes/claims (ADR 0007). Every caller still
sees every tool (allow-all policy) until WP3 lands the Mongo policy; resources and
prompts are hidden. WP3 (Mongo policy + hot reload) and WP4 (Mongo-driven upstreams) plug
into the interfaces in `policy/` and `upstreams/registry.py`.

## Run locally

```bash
make up                      # from the repo root: Mongo, Keycloak, sample upstreams
cd gateway
uv sync                      # Python 3.12 venv with fastmcp==4.1.*
uv run mcp-gateway           # http://localhost:8080/mcp, metrics on :9090
```

Check it:

```bash
curl -s localhost:8080/readyz
GATEWAY_SMOKE_TOKEN=$(make -s -C .. token) uv run python -m mcp_gateway.smoke http://localhost:8080
```

or from the repo root `make smoke` (infra checks, then guest, garbage-bearer and dev1
clients through an in-process gateway; `make smoke GATEWAY_URL=http://localhost:8080`
targets a running one).

Python client (a string `auth=` becomes `Authorization: Bearer ...`):

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as c:                  # guest
    print([t.name for t in await c.list_tools()])   # public_time_now, public_echo, private_secret_lookup
    print((await c.call_tool("public_echo", {"text": "hi"})).data)

async with Client("http://localhost:8080/mcp", auth=token) as c:      # dev1
    print((await c.call_tool("gateway_whoami", {})).data)             # dev only: the resolved principal
```

## Authentication

Bearer auth is optional per request (PLAN section 3.1, ADR 0007) and lives in
`auth/asgi.py` around `/mcp` only:

| Request                                                                                                                                                         | Result                                                                                                                                                                 |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| no `Authorization` header                                                                                                                                       | anonymous principal (401 when `GATEWAY_ALLOW_ANONYMOUS=false`)                                                                                                         |
| `Bearer <jwt>` that verifies                                                                                                                                    | `Principal(kind="user", sub, client_id, scopes, claims)` on `request.state.principal` and `current_principal()`                                                        |
| bad signature, expired, `nbf` in the future, wrong `iss`/`aud`, unknown `kid` (after a throttled JWKS refresh), `alg` not allowed, malformed, non-Bearer scheme | `401 {"error":"invalid_token"}` + `WWW-Authenticate: Bearer resource_metadata="<GATEWAY_PUBLIC_BASE_URL>/.well-known/oauth-protected-resource", error="invalid_token"` |
| token in the query string or a cookie                                                                                                                           | ignored (treated as absent)                                                                                                                                            |

Verification (`auth/jwt.py`, on FastMCP's `JWTVerifier`): `alg` allow-list, `exp`
(required) and `nbf` with a small skew, `iss` = `GATEWAY_OIDC_ISSUER`, `aud` contains
`GATEWAY_JWT_AUDIENCE`. JWKS keys are cached for an hour and refreshed on an unknown `kid`
at most every `GATEWAY_JWKS_REFRESH_FLOOR_SEC`; known keys stay usable while the IdP is
down. Client tokens are never forwarded upstream.

### Getting a token locally

```bash
make token                 # dev1 via the password grant (KC_USER=admin1 for admin1): realm_access.roles ∋ mcp-developer
make token-ci              # ci-runner via client credentials: azp/client_id = ci-runner
make decode TOKEN=...      # print the claims
curl -si -X POST localhost:8080/mcp -H "Authorization: Bearer $(make -s token)" ... # or use the Python client above
```

A token with the wrong audience (for the 401 path) comes from the `other-api` client:
`curl -d client_id=other-api -d client_secret=other-api-secret -d grant_type=client_credentials
http://localhost:8180/realms/mcp/protocol/openid-connect/token`. Keycloak imports
`local/keycloak/realm.json` only on first start; a stack created before WP2 needs
`make clean && make up && make seed` to get that client (integration tests skip it
otherwise).

Each rejection is one `auth.rejected` audit line with a `reason` code; every `tools/call`
audit line carries `principal_kind`, `sub` and `client_id`.

## Configuration (`GATEWAY_*` environment variables)

| Variable                                 | Default                                     | Notes                                                                                                                                                                                         |
| ---------------------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GATEWAY_ENV`                            | `dev`                                       | `prod` enables guards: no stdio upstreams, https base URL, host/origin guard and JSON logs forced                                                                                             |
| `GATEWAY_HOST` / `GATEWAY_PORT`          | `0.0.0.0` / `8080`                          | MCP + health + metrics listener                                                                                                                                                               |
| `GATEWAY_METRICS_PORT`                   | `9090`                                      | Separate Prometheus listener; `0` disables it (`/metrics` stays on the main port)                                                                                                             |
| `GATEWAY_PUBLIC_BASE_URL`                | `http://localhost:8080`                     | External origin, used in RFC 9728 metadata                                                                                                                                                    |
| `GATEWAY_MCP_PATH`                       | `/mcp`                                      |                                                                                                                                                                                               |
| `GATEWAY_ALLOWED_HOSTS`                  | `[]`                                        | JSON list of extra `Host` values accepted by the DNS-rebinding guard (localhost always ok in dev)                                                                                             |
| `GATEWAY_HOST_ORIGIN_PROTECTION`         | `true`                                      |                                                                                                                                                                                               |
| `GATEWAY_ALLOW_ANONYMOUS`                | `true`                                      | `false` = strict mode: requests without a bearer token get 401                                                                                                                                |
| `GATEWAY_OIDC_ISSUER`                    | `http://localhost:8180/realms/mcp`          | Expected `iss`; JWKS URI discovered from `<issuer>/.well-known/openid-configuration` at startup (fatal in prod, retried on demand in dev). Empty = reject every token. https required in prod |
| `GATEWAY_JWKS_URI`                       | unset                                       | Explicit JWKS endpoint, skips discovery (containers: issuer is the host-visible URL, JWKS via the service name)                                                                               |
| `GATEWAY_JWT_AUDIENCE`                   | `mcp-gateway`                               | Required `aud` member                                                                                                                                                                         |
| `GATEWAY_JWT_ALGORITHMS`                 | `RS256`                                     | CSV or JSON allow-list of asymmetric `alg` values from one key family; `none`/`HS*` refused                                                                                                   |
| `GATEWAY_JWT_CLOCK_SKEW_SEC`             | `30`                                        | Leeway on `exp`/`nbf`                                                                                                                                                                         |
| `GATEWAY_JWKS_REFRESH_FLOOR_SEC`         | `300`                                       | Minimum gap between unknown-`kid` JWKS refreshes                                                                                                                                              |
| `GATEWAY_OIDC_DISCOVERY_TIMEOUT_SEC`     | `5`                                         |                                                                                                                                                                                               |
| `GATEWAY_SCOPES_SUPPORTED`               | `[]`                                        | Advertised in the RFC 9728 document (CSV or JSON)                                                                                                                                             |
| `GATEWAY_MONGO_URI` / `GATEWAY_MONGO_DB` | local replica set / `mcp_gateway`           | Used from WP3 on                                                                                                                                                                              |
| `GATEWAY_UPSTREAM_SECRETS_DIR`           | `/etc/gateway/upstream-secrets`             | Used from WP4 on                                                                                                                                                                              |
| `GATEWAY_STATIC_UPSTREAMS`               | public → `:8081/mcp`, private → `:8082/mcp` | WP1 only. JSON list of `{id, url, transport?, cache_ttl_sec?, protocol_mode?}`                                                                                                                |
| `GATEWAY_TOOLS_LIST_TTL_SEC`             | `30`                                        | `ttlMs` cache hint on modern-era list results (ADR 0002)                                                                                                                                      |
| `GATEWAY_LOG_LEVEL` / `GATEWAY_LOG_JSON` | `INFO` / `true`                             | JSON lines to stdout; `GATEWAY_LOG_JSON=false` for a console renderer                                                                                                                         |
| `GATEWAY_TELEMETRY_MODE`                 | `off`                                       | Sets `FASTMCP_TELEMETRY_MODE` (`native` for OTel spans, WP7)                                                                                                                                  |

`protocol_mode` per upstream: `auto` (probe `server/discover`, fall back to the handshake),
`2026-07-28` (pin the modern era, cheapest for FastMCP 4.x upstreams), `legacy`. See ADR 0001.

## Endpoints

| Path                                        | What                                                                                                                                                                                               |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /mcp`                                 | MCP streamable HTTP, both protocol eras, stateless (no GET stream, no sessions)                                                                                                                    |
| `GET /healthz`                              | process up                                                                                                                                                                                         |
| `GET /readyz`                               | 503 until the lifespan finished and all critical readiness probes pass (`health.py`); the `oidc` probe reports the discovered JWKS URI (critical in prod only)                                     |
| `GET /metrics`                              | Prometheus text; also on the metrics port                                                                                                                                                          |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 metadata: `resource`, `authorization_servers=[issuer]`, `bearer_methods_supported=["header"]`, `scopes_supported`; also served at `.../oauth-protected-resource/mcp` (path-suffixed form) |

Metrics: `gateway_auth_results_total{result=anonymous|valid|invalid|disabled_anonymous}`,
`gateway_requests_total{method,era,principal_kind,outcome}`,
`gateway_request_duration_seconds`, `gateway_tool_calls_total{tool,upstream,principal_kind,outcome}`,
`gateway_tool_call_duration_seconds`, `gateway_policy_denials_total`, `gateway_policy_version`,
`gateway_policy_age_seconds`, `gateway_upstream_up{upstream}`, `gateway_build_info`.

## Layout

```
src/mcp_gateway/
  app.py                  Starlette assembly, FastMCP server, middleware chain, lifespan
  main.py                 `mcp-gateway` entry point (uvicorn, one worker)
  settings.py             pydantic-settings, prod guards
  health.py               readiness registry, /healthz /readyz handlers
  telemetry.py            structlog JSON logging
  smoke.py                smoke check: guest, garbage bearer, dev1 (make smoke)
  debug_tools.py          gateway_whoami, registered only when GATEWAY_ENV=dev
  auth/principal.py       Principal (frozen dataclass, cache_key), current_principal()
  auth/context.py         bind_principal(): ContextVar scope used by the ASGI layer
  auth/jwt.py             GatewayJWTVerifier on FastMCP's JWTVerifier, OIDC discovery, JwtAuth lifecycle
  auth/asgi.py            BearerAuthMiddleware around /mcp: anonymous | user | 401 + WWW-Authenticate
  auth/metadata.py        RFC 9728 protected-resource metadata routes
  policy/snapshot.py      PolicySnapshot, ToolFilter, AllowAllSnapshot
  policy/store.py         PolicyStore protocol, ChangeEvent, StaticPolicyStore
  policy/middleware.py    PolicyMiddleware: filters tools/list, blocks tools/call, hides the rest
  upstreams/models.py     UpstreamConfig (mirrors schemas/upstreams.schema.json), CatalogTool, UpstreamHealth
  upstreams/registry.py   UpstreamRegistry: create_proxy + mount(namespace=), apply/catalog/health
  middleware/metrics.py   Prometheus counters and histograms
  middleware/audit.py     one structured line per tools/call
tests/unit                settings, principal, health, policy, metrics/audit, app assembly, auth (in-process IdP)
tests/integration         in-process gateway + FastMCP client against the local stack and Keycloak (skips if down)
```

FastMCP middleware order (outermost first): `PolicyMiddleware`, `AuditMiddleware`,
`MetricsMiddleware`, `ErrorHandlingMiddleware`. The principal is read from
`principal_var` or `request.state.principal`, both set by `BearerAuthMiddleware`.

## Tests

```bash
uv run pytest            # unit + integration (integration skips without `make up`)
uv run pytest -m "not integration"
uv run ruff check . && uv run ruff format --check .
```

## Container

```bash
podman build -t localhost/mcp-gateway:dev -f Containerfile .
podman run --rm -p 8080:8080 -p 9090:9090 --read-only --tmpfs /tmp --cap-drop ALL \
  -e GATEWAY_STATIC_UPSTREAMS='[{"id":"public","url":"http://host.containers.internal:8081/mcp"},{"id":"private","url":"http://host.containers.internal:8082/mcp"}]' \
  localhost/mcp-gateway:dev
```

UBI9 `python-312-minimal`, deps from `uv.lock`, runs as UID 1001 / GID 0 with a read-only
root filesystem (only `/tmp` is written), listens on 8080 and 9090.
