# gateway

Python 3.12 / FastMCP 4.1 MCP gateway: one stateless streamable-HTTP endpoint (`/mcp`)
that proxies upstream MCP servers as namespaced tools and filters them per caller.
Design: [docs/PLAN.md](../docs/PLAN.md) (sections 3 to 5). Verified FastMCP behaviours:
[docs/adr](../docs/adr/README.md).

**WP1 state:** the two sample upstreams are mounted statically from settings, every caller
is anonymous and sees every tool (allow-all policy), resources and prompts are hidden.
WP2 (JWT auth), WP3 (Mongo policy + hot reload) and WP4 (Mongo-driven upstreams) plug into
the interfaces in `auth/principal.py`, `policy/`, `upstreams/registry.py`.

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
uv run python -m mcp_gateway.smoke http://localhost:8080   # guest client: list + call
```

or from the repo root `make smoke` (infra checks, then the guest client through an
in-process gateway; `make smoke GATEWAY_URL=http://localhost:8080` targets a running one).

Python client:

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as c:
    print([t.name for t in await c.list_tools()])   # public_time_now, public_echo, private_secret_lookup
    print((await c.call_tool("public_echo", {"text": "hi"})).data)
```

## Configuration (`GATEWAY_*` environment variables)

| Variable                                 | Default                                     | Notes                                                                                             |
| ---------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `GATEWAY_ENV`                            | `dev`                                       | `prod` enables guards: no stdio upstreams, https base URL, host/origin guard and JSON logs forced |
| `GATEWAY_HOST` / `GATEWAY_PORT`          | `0.0.0.0` / `8080`                          | MCP + health + metrics listener                                                                   |
| `GATEWAY_METRICS_PORT`                   | `9090`                                      | Separate Prometheus listener; `0` disables it (`/metrics` stays on the main port)                 |
| `GATEWAY_PUBLIC_BASE_URL`                | `http://localhost:8080`                     | External origin, used in RFC 9728 metadata                                                        |
| `GATEWAY_MCP_PATH`                       | `/mcp`                                      |                                                                                                   |
| `GATEWAY_ALLOWED_HOSTS`                  | `[]`                                        | JSON list of extra `Host` values accepted by the DNS-rebinding guard (localhost always ok in dev) |
| `GATEWAY_HOST_ORIGIN_PROTECTION`         | `true`                                      |                                                                                                   |
| `GATEWAY_ALLOW_ANONYMOUS`                | `true`                                      | `false` = strict mode, enforced by WP2                                                            |
| `GATEWAY_MONGO_URI` / `GATEWAY_MONGO_DB` | local replica set / `mcp_gateway`           | Used from WP3 on                                                                                  |
| `GATEWAY_UPSTREAM_SECRETS_DIR`           | `/etc/gateway/upstream-secrets`             | Used from WP4 on                                                                                  |
| `GATEWAY_STATIC_UPSTREAMS`               | public → `:8081/mcp`, private → `:8082/mcp` | WP1 only. JSON list of `{id, url, transport?, cache_ttl_sec?, protocol_mode?}`                    |
| `GATEWAY_TOOLS_LIST_TTL_SEC`             | `30`                                        | `ttlMs` cache hint on modern-era list results (ADR 0002)                                          |
| `GATEWAY_LOG_LEVEL` / `GATEWAY_LOG_JSON` | `INFO` / `true`                             | JSON lines to stdout; `GATEWAY_LOG_JSON=false` for a console renderer                             |
| `GATEWAY_TELEMETRY_MODE`                 | `off`                                       | Sets `FASTMCP_TELEMETRY_MODE` (`native` for OTel spans, WP7)                                      |

`protocol_mode` per upstream: `auto` (probe `server/discover`, fall back to the handshake),
`2026-07-28` (pin the modern era, cheapest for FastMCP 4.x upstreams), `legacy`. See ADR 0001.

## Endpoints

| Path                                        | What                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------ |
| `POST /mcp`                                 | MCP streamable HTTP, both protocol eras, stateless (no GET stream, no sessions)      |
| `GET /healthz`                              | process up                                                                           |
| `GET /readyz`                               | 503 until the lifespan finished and all critical readiness probes pass (`health.py`) |
| `GET /metrics`                              | Prometheus text; also on the metrics port                                            |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 placeholder; WP2 fills `authorization_servers`                              |

Metrics: `gateway_requests_total{method,era,principal_kind,outcome}`,
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
  smoke.py                guest smoke check (make smoke)
  auth/principal.py       Principal (frozen dataclass, cache_key), current_principal()
  policy/snapshot.py      PolicySnapshot, ToolFilter, AllowAllSnapshot
  policy/store.py         PolicyStore protocol, ChangeEvent, StaticPolicyStore
  policy/middleware.py    PolicyMiddleware: filters tools/list, blocks tools/call, hides the rest
  upstreams/models.py     UpstreamConfig (mirrors schemas/upstreams.schema.json), CatalogTool, UpstreamHealth
  upstreams/registry.py   UpstreamRegistry: create_proxy + mount(namespace=), apply/catalog/health
  middleware/metrics.py   Prometheus counters and histograms
  middleware/audit.py     one structured line per tools/call
tests/unit                settings, principal, health, policy, metrics/audit, app assembly
tests/integration         in-process gateway + FastMCP client against the local stack (skips if down)
```

FastMCP middleware order (outermost first): `PolicyMiddleware`, `AuditMiddleware`,
`MetricsMiddleware`, `ErrorHandlingMiddleware`. The principal is read from
`principal_var` or `request.state.principal` (WP2 sets them); until then everyone is
anonymous.

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
