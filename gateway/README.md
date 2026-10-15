# gateway

Python 3.12 / FastMCP 4.1 MCP gateway: one stateless streamable-HTTP endpoint (`/mcp`)
that proxies upstream MCP servers as namespaced tools and filters them per caller.
Design: [docs/PLAN.md](../docs/PLAN.md) (sections 3 to 5). Verified FastMCP behaviours:
[docs/adr](../docs/adr/README.md).

**State after WP1 + WP4:** upstreams come from the `upstreams` collection in Mongo
(`make seed` loads `sample-public` and `sample-private`), are mounted as namespaced proxies
with the gateway's own credentials, probed in the background and reconciled live
(mount/unmount/re-mount on config changes). Every caller is still anonymous and sees every
tool (allow-all policy) until WP2 (JWT auth) and WP3 (Mongo policy + hot reload) land;
resources and prompts are hidden. `/internal/*` serves the admin API (WP5).

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
GATEWAY_INTERNAL_TOKEN=dev uv run python -m mcp_gateway.smoke http://localhost:8080   # guest client + catalog
```

or from the repo root `make smoke` (infra checks, then the guest client and
`/internal/catalog` through an in-process gateway reading Mongo;
`make smoke GATEWAY_URL=http://localhost:8080 GATEWAY_INTERNAL_TOKEN=...` targets a running one).

Python client:

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as c:
    print([t.name for t in await c.list_tools()])
    # sample-public_time_now, sample-public_echo, sample-private_secret_lookup
    print((await c.call_tool("sample-public_echo", {"text": "hi"})).data)
```

## Upstreams (WP4)

Each document in `upstreams` (schema: `schemas/upstreams.schema.json`) becomes one
FastMCP proxy mounted under its `_id` as namespace, so upstream tool `echo` on
`sample-public` is `sample-public_echo`. What the registry does with the fields:

| Field            | Effect                                                                                                                                                |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled: false` | unmounted live (tools disappear); re-enabling mounts again                                                                                            |
| `transport`      | `http` (streamable HTTP), `sse`, or `stdio` (dev only: refused with `GATEWAY_ENV=prod`, reported as `degraded`)                                       |
| `auth`           | `none`; `static_bearer` sends `Authorization: Bearer <secret>`; `static_header` sends `<headerName>: <secret>`; `client_credentials` is v2 (degraded) |
| `auth.secretRef` | read from `GATEWAY_UPSTREAM_SECRETS_DIR/<secretRef>` (K8s Secret volume), else `GATEWAY_UPSTREAM_SECRET_<REF>`; missing -> `degraded(secret_missing)` |
| `sessionMode`    | `shared` = one connected client per process, only honoured for `http` + `protocolMode: 2026-07-28`; otherwise forced to `per_request` and logged      |
| `timeoutsMs`     | `connect` -> httpx connect/pool timeout + protocol negotiation deadline; `call` -> per-request deadline (ADR 0009)                                    |
| `cacheTtlSec`    | how long the proxy caches the upstream's tool list for `tools/call` lookups                                                                           |
| `protocolMode`   | not in the schema yet: `auto` (probe) / `2026-07-28` / `legacy`; static upstreams can set it (`protocol_mode`)                                        |

Degraded upstreams (not mounted, with a `reason`) and probe results are visible on
`/internal/upstreams/health` and in `/readyz` detail; they never crash the process and are
retried on the next reload. A probe failure marks the upstream `down`
(`gateway_upstream_up 0`) but keeps it mounted: `tools/list` still answers for the others,
calls to it fail after its connect timeout with `Client failed to connect`.

Until WP3's watcher drives `UpstreamRegistry.apply(snapshot)`, the list is read at startup
and on `POST /internal/upstreams/reload`. `GATEWAY_UPSTREAMS_SOURCE=static` keeps WP1's
`GATEWAY_STATIC_UPSTREAMS` behaviour (no Mongo needed; used by the unit tests).

## Configuration (`GATEWAY_*` environment variables)

| Variable                                    | Default                                     | Notes                                                                                             |
| ------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `GATEWAY_ENV`                               | `dev`                                       | `prod` enables guards: no stdio upstreams, https base URL, host/origin guard and JSON logs forced |
| `GATEWAY_HOST` / `GATEWAY_PORT`             | `0.0.0.0` / `8080`                          | MCP + health + metrics listener                                                                   |
| `GATEWAY_METRICS_PORT`                      | `9090`                                      | Separate Prometheus listener; `0` disables it (`/metrics` stays on the main port)                 |
| `GATEWAY_PUBLIC_BASE_URL`                   | `http://localhost:8080`                     | External origin, used in RFC 9728 metadata                                                        |
| `GATEWAY_MCP_PATH`                          | `/mcp`                                      |                                                                                                   |
| `GATEWAY_ALLOWED_HOSTS`                     | `[]`                                        | JSON list of extra `Host` values accepted by the DNS-rebinding guard (localhost always ok in dev) |
| `GATEWAY_HOST_ORIGIN_PROTECTION`            | `true`                                      |                                                                                                   |
| `GATEWAY_ALLOW_ANONYMOUS`                   | `true`                                      | `false` = strict mode, enforced by WP2                                                            |
| `GATEWAY_MONGO_URI` / `GATEWAY_MONGO_DB`    | local replica set / `mcp_gateway`           | `upstreams` collection (readConcern majority); WP3 adds policy                                    |
| `GATEWAY_MONGO_SERVER_SELECTION_TIMEOUT_MS` | `5000`                                      | How long the upstream loader waits for Mongo                                                      |
| `GATEWAY_UPSTREAMS_SOURCE`                  | `mongo`                                     | `static` = mount `GATEWAY_STATIC_UPSTREAMS` instead (WP1 behaviour)                               |
| `GATEWAY_STATIC_UPSTREAMS`                  | public → `:8081/mcp`, private → `:8082/mcp` | JSON list of `{id, url, transport?, cache_ttl_sec?, protocol_mode?, session_mode?}`               |
| `GATEWAY_UPSTREAM_SECRETS_DIR`              | `/etc/gateway/upstream-secrets`             | `auth.secretRef` files; env fallback `GATEWAY_UPSTREAM_SECRET_<REF>`                              |
| `GATEWAY_UPSTREAM_PROBE_SEC`                | `30`                                        | Health probe interval (`tools/list` per upstream on a fresh short-lived client)                   |
| `GATEWAY_UPSTREAM_PROBE_TIMEOUT_SEC`        | `5`                                         | Deadline of one probe                                                                             |
| `GATEWAY_READY_REQUIRES_UPSTREAM`           | `true`                                      | `/readyz` 503 until at least one upstream is mounted (a dead-but-mounted one still counts)        |
| `GATEWAY_INTERNAL_TOKEN`                    | unset                                       | Enables `/internal/*`; callers send it as `X-Internal-Token`. Unset = 404                         |
| `GATEWAY_TOOLS_LIST_TTL_SEC`                | `30`                                        | `ttlMs` cache hint on modern-era list results (ADR 0002)                                          |
| `GATEWAY_LOG_LEVEL` / `GATEWAY_LOG_JSON`    | `INFO` / `true`                             | JSON lines to stdout; `GATEWAY_LOG_JSON=false` for a console renderer                             |
| `GATEWAY_TELEMETRY_MODE`                    | `off`                                       | Sets `FASTMCP_TELEMETRY_MODE` (`native` for OTel spans, WP7)                                      |

`protocol_mode` per upstream: `auto` (probe `server/discover`, fall back to the handshake),
`2026-07-28` (pin the modern era, cheapest for FastMCP 4.x upstreams), `legacy`. See ADR 0001.

## Endpoints

| Path                                        | What                                                                                                 |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `POST /mcp`                                 | MCP streamable HTTP, both protocol eras, stateless (no GET stream, no sessions)                      |
| `GET /healthz`                              | process up                                                                                           |
| `GET /readyz`                               | 503 until the lifespan finished and all critical readiness probes pass (`health.py`)                 |
| `GET /metrics`                              | Prometheus text; also on the metrics port                                                            |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 placeholder; WP2 fills `authorization_servers`                                              |
| `GET /internal/catalog`                     | every tool across mounted upstreams (`namespaced_name, upstream, name, description, input_schema`)   |
| `GET /internal/upstreams/health`            | last probe result per upstream (`status up/down/degraded/unknown`, `reason`, `latency_ms`)           |
| `POST /internal/upstreams/reload`           | re-read the upstream list and reconcile (interim until WP3)                                          |
| `GET /internal/effective-tools`             | tools a principal sees: `?kind=anonymous` or `?claims=<base64url JSON>`; 501 until WP3 implements it |

`/internal/*` requires `X-Internal-Token` equal to `GATEWAY_INTERNAL_TOKEN` (constant-time
compare, 403 otherwise, 404 when unset) and is served by the outer Starlette app, never via
`/mcp`. In OpenShift a NetworkPolicy restricts it to the admin-api pod.

Metrics: `gateway_requests_total{method,era,principal_kind,outcome}`,
`gateway_request_duration_seconds`, `gateway_tool_calls_total{tool,upstream,principal_kind,outcome}`,
`gateway_tool_call_duration_seconds`, `gateway_policy_denials_total`, `gateway_policy_version`,
`gateway_policy_age_seconds`, `gateway_upstream_up{upstream}`,
`gateway_upstream_probe_seconds{upstream}`, `gateway_upstream_errors_total{upstream,kind}`,
`gateway_upstreams_mounted`, `gateway_build_info`.

## Layout

```
src/mcp_gateway/
  app.py                  Starlette assembly, FastMCP server, middleware chain, lifespan
  main.py                 `mcp-gateway` entry point (uvicorn, one worker)
  settings.py             pydantic-settings, prod guards
  health.py               readiness registry, /healthz /readyz handlers
  internal_routes.py      /internal/* (catalog, upstream health/reload, effective-tools), token guard
  telemetry.py            structlog JSON logging
  smoke.py                guest + catalog smoke check (make smoke)
  auth/principal.py       Principal (frozen dataclass, cache_key), current_principal()
  policy/snapshot.py      PolicySnapshot, ToolFilter, AllowAllSnapshot
  policy/store.py         PolicyStore protocol, ChangeEvent, StaticPolicyStore
  policy/middleware.py    PolicyMiddleware: filters tools/list, blocks tools/call, hides the rest
  upstreams/models.py     UpstreamConfig (mirrors schemas/upstreams.schema.json), CatalogTool, UpstreamHealth
  upstreams/secrets.py    secretRef -> file in GATEWAY_UPSTREAM_SECRETS_DIR, env fallback
  upstreams/proxy.py      build_proxy: transport + static auth + timeouts + session mode (ADR 0001/0009)
  upstreams/registry.py   UpstreamRegistry: apply() diff/mount/unmount, catalog(), health(), probe loop
  upstreams/source.py     where the desired list comes from: Mongo `upstreams` or static settings
  middleware/metrics.py   Prometheus counters and histograms
  middleware/audit.py     one structured line per tools/call
tests/unit                settings, principal, health, policy, metrics/audit, app assembly,
                          upstream models/secrets/proxy/registry (fake FastMCP + ADR 0005 guard), internal routes
tests/integration         in-process gateway + FastMCP client against the local stack (skips if down);
                          test_upstreams_live.py needs Mongo and `podman` (stops/starts mcp-sample-private)
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
  -e GATEWAY_MONGO_URI='mongodb://root:devpass@host.containers.internal:27017/?replicaSet=rs0&directConnection=true' \
  localhost/mcp-gateway:dev
```

Inside a container the seed's `localhost:8081/8082` upstream URLs are not reachable; point
the documents at `host.containers.internal` (or the compose service names) and
`POST /internal/upstreams/reload`, or run with `GATEWAY_UPSTREAMS_SOURCE=static` and
`GATEWAY_STATIC_UPSTREAMS` as in WP1.

UBI9 `python-312-minimal`, deps from `uv.lock`, runs as UID 1001 / GID 0 with a read-only
root filesystem (only `/tmp` is written), listens on 8080 and 9090.
