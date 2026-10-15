# gateway

Python 3.12 / FastMCP 4.1 MCP gateway: one stateless streamable-HTTP endpoint (`/mcp`)
that proxies upstream MCP servers as namespaced tools and filters them per caller.
Design: [docs/PLAN.md](../docs/PLAN.md) (sections 3 to 5). Verified FastMCP behaviours:
[docs/adr](../docs/adr/README.md).

**State after WP3:** the two sample upstreams are mounted statically from settings
(ids `sample-public` / `sample-private`, matching `local/seed`), the per-caller allow-lists
come from MongoDB and are hot-reloaded (change stream + poll, see
[Policy](#policy-allow-lists-from-mongodb)), resources and prompts are hidden. Until WP2
lands every caller is anonymous and gets the guest floor (`public-basics` in the seed);
WP4 will mount upstreams from the snapshot instead of settings.

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
uv run python -m mcp_gateway.smoke http://localhost:8080   # guest list/call + live toolset edit
```

or from the repo root `make smoke` (infra checks, then the guest client through an
in-process gateway, including an allow-list edit in Mongo that must show up within 2 s;
`make smoke GATEWAY_URL=http://localhost:8080` targets a running one). `make seed` first.

Python client:

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as c:
    print([t.name for t in await c.list_tools()])  # sample-public_time_now, sample-public_echo
    print((await c.call_tool("sample-public_echo", {"text": "hi"})).data)
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
| `GATEWAY_MONGO_URI` / `GATEWAY_MONGO_DB` | local replica set / `mcp_gateway`           | Policy store; needs a replica set (change streams)                                                |
| `GATEWAY_POLICY_SOURCE`                  | `mongo`                                     | `static` = allow-all, dev/test only (refused in prod)                                             |
| `GATEWAY_POLICY_POLL_SEC`                | `30`                                        | Fallback poll of `meta.version` (covers missed events / tiers without change streams)             |
| `GATEWAY_POLICY_DEBOUNCE_MS`             | `250`                                       | Change-stream events within this window cause one reload                                          |
| `GATEWAY_UPSTREAM_SECRETS_DIR`           | `/etc/gateway/upstream-secrets`             | Used from WP4 on                                                                                  |
| `GATEWAY_STATIC_UPSTREAMS`               | `sample-public` → `:8081/mcp`, `sample-private` → `:8082/mcp` | Until WP4. JSON list of `{id, url, transport?, cache_ttl_sec?, protocol_mode?}`; `id` is the tool namespace and must match the ids the toolsets use |
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
| `GET /readyz`                               | 503 until the lifespan finished and a policy snapshot is loaded; JSON reports `checks.policy` (version, `policy_age_seconds`, change stream state) |
| `GET /metrics`                              | Prometheus text; also on the metrics port                                            |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 placeholder; WP2 fills `authorization_servers`                              |

Metrics: `gateway_requests_total{method,era,principal_kind,outcome}`,
`gateway_request_duration_seconds`, `gateway_tool_calls_total{tool,upstream,principal_kind,outcome}`,
`gateway_tool_call_duration_seconds`, `gateway_policy_denials_total{tool,principal_kind}`,
`gateway_policy_version`, `gateway_policy_age_seconds`, `gateway_policy_reloads_total{reason}`,
`gateway_policy_reload_failures_total`, `gateway_policy_invalid_documents{collection}`,
`gateway_change_stream_up`, `gateway_upstream_up{upstream}`, `gateway_build_info`.

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
  telemetry.py            structlog JSON logging
  smoke.py                guest smoke check incl. live allow-list edit (make smoke)
  auth/principal.py       Principal (frozen dataclass, cache_key), current_principal()
  policy/models.py        pydantic models for toolsets/grants/meta (mirror schemas/)
  policy/matcher.py       subject matching (claim paths, ops) and GlobToolFilter
  policy/snapshot.py      PolicySnapshot (effective_tools, matching_grants), build_snapshot, AllowAllSnapshot
  policy/store.py         PolicyStore protocol, ChangeEvent, MongoPolicyStore (load + change stream + poll), StaticPolicyStore
  policy/watcher.py       PolicyHolder (atomic swap), PolicyWatcher (debounce, reload, list_changed, metrics, probes)
  policy/middleware.py    PolicyMiddleware: filters tools/list, blocks tools/call, hides the rest
  upstreams/models.py     UpstreamConfig (mirrors schemas/upstreams.schema.json), CatalogTool, UpstreamHealth
  upstreams/registry.py   UpstreamRegistry: create_proxy + mount(namespace=), apply/catalog/health
  middleware/metrics.py   Prometheus counters and histograms
  middleware/audit.py     one structured line per tools/call
tests/unit                settings, principal, health, policy models/matcher (hypothesis)/watcher/store/middleware, metrics/audit, app assembly
tests/integration         in-process gateway + FastMCP client against the local stack (skips if down):
                          static policy (test_gateway_live), Mongo policy with live edits, listen stream,
                          poll fallback and replica-set step-down (test_policy_live)
```

FastMCP middleware order (outermost first): `PolicyMiddleware`, `AuditMiddleware`,
`MetricsMiddleware`, `ErrorHandlingMiddleware`. The principal is read from
`principal_var` or `request.state.principal` (WP2 sets them); until then everyone is
anonymous. The SDK `ListenHandler` is registered on the low-level server in `app.py`
(ADR 0003) so `subscriptions/listen` works and `tools.listChanged` is advertised.

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
  -e GATEWAY_STATIC_UPSTREAMS='[{"id":"public","url":"http://host.containers.internal:8081/mcp"},{"id":"private","url":"http://host.containers.internal:8082/mcp"}]' \
  localhost/mcp-gateway:dev
```

UBI9 `python-312-minimal`, deps from `uv.lock`, runs as UID 1001 / GID 0 with a read-only
root filesystem (only `/tmp` is written), listens on 8080 and 9090.
