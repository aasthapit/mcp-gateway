# gateway

Python 3.12 / FastMCP 4.1 MCP gateway: one stateless streamable-HTTP endpoint (`/mcp`)
that proxies upstream MCP servers as namespaced tools and filters them per caller.
Design: [docs/PLAN.md](../docs/PLAN.md) (sections 3 to 5). Verified FastMCP behaviours:
[docs/adr](../docs/adr/README.md).

**State after WP2 + WP3 + WP4:** upstreams come from the `upstreams` collection in Mongo
(`make seed` loads `sample-public` and `sample-private`), are mounted as namespaced proxies
with the gateway's own credentials, probed in the background and reconciled live on every
policy reload (mount/unmount/re-mount on config changes). Callers are anonymous or, with a
valid `Authorization: Bearer <jwt>` from the configured OIDC issuer, users with
`sub`/`client_id`/scopes/claims (ADR 0007). The per-caller allow-lists come from MongoDB
and are hot-reloaded (change stream + poll, see [Policy](#policy-allow-lists-from-mongodb));
the guest floor is `public-basics` in the seed and `mcp-developer` users also get
`engineering`. Resources and prompts are hidden. `/internal/*` serves the admin API (WP5).

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
GATEWAY_SMOKE_TOKEN=$(make -s -C .. token) GATEWAY_INTERNAL_TOKEN=dev uv run python -m mcp_gateway.smoke http://localhost:8080
```

or from the repo root `make smoke` (infra checks, then guest, garbage-bearer and dev1
clients through an in-process gateway reading Mongo, including an allow-list edit that
must show up within 2 s and `/internal/catalog`; `make smoke GATEWAY_URL=http://localhost:8080
GATEWAY_INTERNAL_TOKEN=...` targets a running one). `make seed` first.

Python client (a string `auth=` becomes `Authorization: Bearer ...`):

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as c:                  # guest
    print([t.name for t in await c.list_tools()])  # sample-public_time_now, sample-public_echo
    print((await c.call_tool("sample-public_echo", {"text": "hi"})).data)

async with Client("http://localhost:8080/mcp", auth=token) as c:      # dev1
    print([t.name for t in await c.list_tools()])  # + the `engineering` toolset
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

The policy watcher drives `UpstreamRegistry.apply(snapshot)` on every reload, so an
upstream document change takes effect live; `POST /internal/upstreams/reload` forces a
reload. `GATEWAY_UPSTREAMS_SOURCE=static` keeps WP1's `GATEWAY_STATIC_UPSTREAMS`
behaviour (no Mongo needed; used by the unit tests) and reloads leave those mounts alone.

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
| `GATEWAY_MONGO_URI` / `GATEWAY_MONGO_DB` | local replica set / `mcp_gateway`           | Policy store and `upstreams` collection (readConcern majority); needs a replica set (change streams) |
| `GATEWAY_MONGO_SERVER_SELECTION_TIMEOUT_MS` | `5000`                                      | How long the upstream loader waits for Mongo                                                      |
| `GATEWAY_POLICY_SOURCE`                  | `mongo`                                     | `static` = allow-all, dev/test only (refused in prod)                                             |
| `GATEWAY_POLICY_POLL_SEC`                | `30`                                        | Fallback poll of `meta.version` (covers missed events / tiers without change streams)             |
| `GATEWAY_POLICY_DEBOUNCE_MS`             | `250`                                       | Change-stream events within this window cause one reload                                          |
| `GATEWAY_UPSTREAMS_SOURCE`                  | `mongo`                                     | `static` = mount `GATEWAY_STATIC_UPSTREAMS` instead (WP1 behaviour); with `mongo` the policy snapshot drives the mounts                               |
| `GATEWAY_STATIC_UPSTREAMS`                  | public → `:8081/mcp`, private → `:8082/mcp` | JSON list of `{id, url, transport?, cache_ttl_sec?, protocol_mode?, session_mode?}`               |
| `GATEWAY_UPSTREAM_SECRETS_DIR`              | `/etc/gateway/upstream-secrets`             | `auth.secretRef` files; env fallback `GATEWAY_UPSTREAM_SECRET_<REF>`                              |
| `GATEWAY_UPSTREAM_PROBE_SEC`                | `30`                                        | Health probe interval (`tools/list` per upstream on a fresh short-lived client)                   |
| `GATEWAY_UPSTREAM_PROBE_TIMEOUT_SEC`        | `5`                                         | Deadline of one probe                                                                             |
| `GATEWAY_READY_REQUIRES_UPSTREAM`           | `true`                                      | `/readyz` 503 until at least one upstream is mounted (a dead-but-mounted one still counts)        |
| `GATEWAY_INTERNAL_TOKEN`                    | unset                                       | Enables `/internal/*`; callers send it as `X-Internal-Token`. Unset = 404                         |
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
| `GET /readyz`                               | 503 until the lifespan finished, a policy snapshot is loaded and all critical readiness probes pass (`health.py`); JSON reports `checks.policy` (version, `policy_age_seconds`, change stream state) and the `oidc` probe (critical in prod only) |
| `GET /metrics`                              | Prometheus text; also on the metrics port                                                                                                                                                          |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 metadata: `resource`, `authorization_servers=[issuer]`, `bearer_methods_supported=["header"]`, `scopes_supported`; also served at `.../oauth-protected-resource/mcp` (path-suffixed form) |
| `GET /internal/catalog`                     | every tool across mounted upstreams (`namespaced_name, upstream, name, description, input_schema`)   |
| `GET /internal/upstreams/health`            | last probe result per upstream (`status up/down/degraded/unknown`, `reason`, `latency_ms`)           |
| `POST /internal/upstreams/reload`           | force a policy reload and reconcile the upstreams (the watcher does this on every change)                                          |
| `GET /internal/effective-tools`             | tools a principal sees: `?kind=anonymous` or `?claims=<base64url JSON>`; also returns the matching grants |

`/internal/*` requires `X-Internal-Token` equal to `GATEWAY_INTERNAL_TOKEN` (constant-time
compare, 403 otherwise, 404 when unset) and is served by the outer Starlette app, never via
`/mcp`. In OpenShift a NetworkPolicy restricts it to the admin-api pod.

Metrics: `gateway_auth_results_total{result=anonymous|valid|invalid|disabled_anonymous}`,
`gateway_requests_total{method,era,principal_kind,outcome}`,
`gateway_request_duration_seconds`, `gateway_tool_calls_total{tool,upstream,principal_kind,outcome}`,
`gateway_tool_call_duration_seconds`, `gateway_policy_denials_total{tool,principal_kind}`,
`gateway_policy_version`, `gateway_policy_age_seconds`, `gateway_policy_reloads_total{reason}`,
`gateway_policy_reload_failures_total`, `gateway_policy_invalid_documents{collection}`,
`gateway_change_stream_up`, `gateway_upstream_up{upstream}`,
`gateway_upstream_probe_seconds{upstream}`, `gateway_upstream_errors_total{upstream,kind}`,
`gateway_upstreams_mounted`, `gateway_build_info`.

## Policy: allow-lists from MongoDB

Collections `upstreams`, `toolsets`, `grants`, `meta` in `GATEWAY_MONGO_DB`
(schemas in `../schemas`, pydantic mirrors in `policy/models.py`). Evaluation per request
(`policy/snapshot.py`, `policy/matcher.py`), as in docs/PLAN.md 3.2:

- a grant matches by subject: `anonymous` (everyone, the guest floor), `authenticated`
  (any valid token), `claim` (`path` dotted into the claims, `op` eq/in/contains/exists;
  `contains` also understands space-separated string claims such as `scope`),
  `client_id` (`Principal.client_id`, else the `azp`/`client_id` claim);
- effective tools = union of `allow` of the enabled toolsets of all matching enabled
  grants, minus the union of their `deny`; fnmatch globs on namespaced names
  (`<upstream-id>_<tool>`); deny by default; memoised per `Principal.cache_key`;
- `tools/list` is filtered, a `tools/call` on a hidden tool answers exactly like an
  unknown tool (`Unknown tool: '<name>'`) and counts in `gateway_policy_denials_total`.

How the reload works (`policy/store.py`, `policy/watcher.py`, ADR 0008):

1. Startup: `load()` reads the four collections with `readConcern: majority` (re-reading
   if `meta.version` moved meanwhile), builds an immutable `PolicySnapshot`, swaps it into
   the `PolicyHolder` the middleware reads. `/readyz` is 503 until that first load
   succeeds; if Mongo is down the gateway keeps retrying in the background and serves
   nothing meanwhile (deny by default).
2. One database change stream per process, filtered to the four collections,
   `fullDocument: updateLookup`; any event is debounced 250 ms into one full reload and
   an atomic swap. The resume token is kept in memory: resumable errors reopen from it,
   `ChangeStreamHistoryLost` / `invalidate` drop it and reload fully. Every (re)open
   without a token also triggers a reload, so nothing between the last load and the
   stream start is missed.
3. A `GATEWAY_POLICY_POLL_SEC` poll of `meta.version` reloads whenever the stored version
   differs from the loaded one: covers a dead stream and Atlas tiers without change
   streams, at poll latency. Admin writes must bump `meta.version` (same transaction) for
   the poll to notice them; the stream notices any write.
4. After a swap the watcher runs `registry.apply(snapshot)` (new upstreams, WP4) and
   publishes `ToolsListChanged` on the in-process subscription bus, so modern clients with
   an open `subscriptions/listen` stream get `notifications/tools/list_changed`
   (ADR 0003). Others re-list when `ttlMs` expires (ADR 0002).
5. A failed reload keeps the previous snapshot and the pod stays ready; `/readyz` shows
   `policy_age_seconds` and `last_error`, `gateway_policy_age_seconds` keeps growing, and
   the non-critical `policy_stream` check shows the change-stream state. Alert on age.

Measured on the local stack: an edit shows in the next `tools/list` after about 0.4 s
(250 ms debounce + load); a forced replica-set step-down is absorbed by pymongo's own
resume without the gateway noticing; a full Mongo outage leaves the stale snapshot in
service with reconnect attempts backing off to 10 s.

## Layout

```
src/mcp_gateway/
  app.py                  Starlette assembly, FastMCP server, middleware chain, lifespan
  main.py                 `mcp-gateway` entry point (uvicorn, one worker)
  settings.py             pydantic-settings, prod guards
  health.py               readiness registry, /healthz /readyz handlers
  internal_routes.py      /internal/* (catalog, upstream health/reload, effective-tools), token guard
  telemetry.py            structlog JSON logging
  smoke.py                smoke check: guest, garbage bearer, live allow-list edit, catalog, dev1 (make smoke)
  debug_tools.py          gateway_whoami, registered only when GATEWAY_ENV=dev
  auth/principal.py       Principal (frozen dataclass, cache_key), current_principal()
  auth/context.py         bind_principal(): ContextVar scope used by the ASGI layer
  auth/jwt.py             GatewayJWTVerifier on FastMCP's JWTVerifier, OIDC discovery, JwtAuth lifecycle
  auth/asgi.py            BearerAuthMiddleware around /mcp: anonymous | user | 401 + WWW-Authenticate
  auth/metadata.py        RFC 9728 protected-resource metadata routes
  policy/models.py        pydantic models for toolsets/grants/meta (mirror schemas/)
  policy/matcher.py       subject matching (claim paths, ops) and GlobToolFilter
  policy/snapshot.py      PolicySnapshot (effective_tools, matching_grants), build_snapshot, AllowAllSnapshot
  policy/store.py         PolicyStore protocol, ChangeEvent, MongoPolicyStore (load + change stream + poll), StaticPolicyStore
  policy/watcher.py       PolicyHolder (atomic swap), PolicyWatcher (debounce, reload, list_changed, metrics, probes)
  policy/middleware.py    PolicyMiddleware: filters tools/list, blocks tools/call, hides the rest
  upstreams/models.py     UpstreamConfig (mirrors schemas/upstreams.schema.json), CatalogTool, UpstreamHealth
  upstreams/secrets.py    secretRef -> file in GATEWAY_UPSTREAM_SECRETS_DIR, env fallback
  upstreams/proxy.py      build_proxy: transport + static auth + timeouts + session mode (ADR 0001/0009)
  upstreams/registry.py   UpstreamRegistry: apply() diff/mount/unmount, catalog(), health(), probe loop
  upstreams/source.py     where the desired list comes from: Mongo `upstreams` or static settings
  middleware/metrics.py   Prometheus counters and histograms
  middleware/audit.py     one structured line per tools/call
tests/unit                settings, principal, health, policy models/matcher (hypothesis)/watcher/store/middleware, metrics/audit, app assembly,
                          auth (in-process IdP), upstream models/secrets/proxy/registry (fake FastMCP + ADR 0005 guard), internal routes
tests/integration         in-process gateway + FastMCP client against the local stack and Keycloak (skips if down):
                          static policy (test_gateway_live), auth flows (test_auth_live), Mongo policy with live
                          edits, listen stream, poll fallback and replica-set step-down (test_policy_live);
                          test_upstreams_live.py needs Mongo and `podman` (stops/starts mcp-sample-private)
```

FastMCP middleware order (outermost first): `PolicyMiddleware`, `AuditMiddleware`,
`MetricsMiddleware`, `ErrorHandlingMiddleware`. The principal is read from
`principal_var` or `request.state.principal`, both set by `BearerAuthMiddleware`. The SDK
`ListenHandler` is registered on the low-level server in `app.py` (ADR 0003) so
`subscriptions/listen` works and `tools.listChanged` is advertised.

## Tests

```bash
uv run pytest            # unit + integration (integration skips without `make up` + `make seed`)
uv run pytest -m "not integration"
uv run ruff check . && uv run ruff format --check .
```

The Mongo-policy integration tests re-seed the policy collections from `local/seed`
before and after they run.

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
