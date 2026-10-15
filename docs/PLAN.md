# MCP Gateway — Research & Implementation Plan

Status: proposal, 2026-10-14. Research sources are cited inline; items marked **[verify]** were not confirmed against primary sources and must be checked by the implementing worker before relying on them.

## 1. Goal

One MCP endpoint (`https://mcp.<domain>/mcp`) that:

1. Proxies 1..n upstream MCP servers behind a single server identity.
2. Exposes a **guest** tool set to unauthenticated clients, and **additional** tool sets to clients presenting a valid JWT bearer token, decided per request.
3. Reads the allow-lists (which tools, for whom) from a MongoDB collection that an admin UI (Node) edits through an admin API; changes take effect on every running gateway replica within seconds, without a restart.
4. Runs as N identical replicas on OpenShift with no sticky sessions, no shared in-process state, and standard enterprise hygiene (restricted SCC, TLS, secrets management, metrics, tracing, audit, HPA, PDB, NetworkPolicy).

Non-goals for v1: user-specific upstream credentials (token exchange), resources/prompts exposure (hidden by policy), global rate-limiting across replicas, an OAuth authorization server (the gateway is a resource server only).

## 2. Research findings that drive the design

| Finding | Source | Consequence |
|---|---|---|
| Current MCP spec revision is **2026-07-28**; it **removed protocol-level sessions** and the `Mcp-Session-Id` header. Every request is self-contained (`_meta` carries version/capabilities). Server push is via a client-opened `subscriptions/listen` SSE stream. Tool lists "MAY vary by the authorization presented on the request". `tools/list` results carry `ttlMs` + `cacheScope`. | modelcontextprotocol.io/specification/2026-07-28 (changelog, transports, tools) | Per-request tool filtering is spec-sanctioned. Replicas need no affinity. Legacy clients (2025-xx) still send `initialize`; we serve them statelessly too. |
| FastMCP (Python) is at **4.1.0** (2026-10-08); built on MCP SDK v2; supports both the modern and the legacy era. `create_proxy()`, `mount(namespace=)`, providers/transforms, `Middleware` with `on_list_tools`/`on_call_tool`, native OpenTelemetry tracing. | github.com/PrefectHQ/fastmcp releases; gofastmcp.com docs | Build on FastMCP 4.1, not 2.x/3.x APIs. |
| FastMCP **does not support optional auth**. With `auth=` set, requests without a bearer are rejected (401) at the ASGI layer before any MCP method runs; the maintainer closed the feature request as wontfix (issue #3291). | gofastmcp.com/servers/auth; PrefectHQ/fastmcp#3291 | Run the FastMCP server with `auth=None` and implement JWT verification + principal resolution in our own ASGI middleware, policy in our own FastMCP middleware. |
| MCP auth: tokens only in `Authorization: Bearer`; gateway **MUST** validate `aud` (RFC 8707); gateway **MUST NOT** pass the client's token through to upstreams; **MUST** publish RFC 9728 protected-resource metadata. Authorization itself is OPTIONAL. | spec 2026-07-28/basic/authorization | Guest access is allowed; invalid tokens are a hard 401; upstream credentials are the gateway's own. |
| FastMCP proxies open a **fresh upstream session per request** by default; a shared client is allowed only for stateless HTTP upstreams. Inbound `Authorization` is **forwarded upstream by default** (`forward_incoming_headers=True`). Proxy tool lists are cached 300 s (`cache_ttl`). Upstream failures become `INTERNAL_ERROR` for a call, and a failed provider is skipped (`provider_error_strategy="warn"`) in aggregate listings. | gofastmcp.com/servers/proxy | Disable header forwarding. Use a shared client for stateless upstreams. One bad upstream must not take the whole list down. |
| `stateless_http=True` is required for multi-worker/multi-replica legacy-era serving; in that mode GET SSE streams are disabled, so legacy clients get no push `list_changed`. Modern-era clients get push via `subscriptions/listen`. | gofastmcp.com/deployment/http | Accept: legacy clients re-list; modern clients are notified per pod. |
| Mongo **change streams require a replica set**; one stream per process; resume tokens survive failover; majority-committed only. | mongodb.com/docs/manual/changeStreams | Local dev needs a single-node replica set. Each pod runs one watcher plus a version-poll fallback. |
| OpenShift `restricted-v2`/`v3` SCC: arbitrary UID, no privilege escalation, all caps dropped; ports < 1024 forbidden. Router idle timeout defaults to 30 s (`haproxy.router.openshift.io/timeout` raises it). The MongoDB Community Operator is archived; successors are **MongoDB Controllers for Kubernetes (MCK)** and the **Percona Operator for MongoDB** (Red Hat certified). External Secrets Operator for OpenShift is available on 4.20+. | docs.redhat.com; github.com/mongodb; docs.percona.com | UBI9 python image, port 8080, SSE keep-alives, route timeout annotation, Percona operator for prod Mongo. |

## 3. Architecture

```
                    ┌──────────────────────── OpenShift namespace: mcp-gateway ────────────────────────┐
  MCP clients       │   Route (edge TLS, timeout=1h)                                                  │
  (guest or JWT) ──►│ ─► Service ─► Deployment mcp-gateway  × N (HPA 3..10, PDB minAvailable=2)        │
                    │        ┌──────────────────────────────────────────────────────────────┐          │
                    │        │ uvicorn (1 worker)                                           │          │
                    │        │  Starlette app                                               │          │
                    │        │   ├ /healthz /readyz /metrics                                │          │
                    │        │   ├ /.well-known/oauth-protected-resource   (RFC 9728)       │          │
                    │        │   ├ /internal/catalog, /internal/effective-tools (NetPol-only)│          │
                    │        │   └ /mcp  ◄─ AuthMiddleware (ASGI): Bearer? → verify JWKS   │          │
                    │        │              │                  none  → Principal(guest)     │          │
                    │        │              │                  bad   → 401 + WWW-Authenticate│          │
                    │        │              ▼                                               │          │
                    │        │        FastMCP("gateway", auth=None, stateless_http=True)    │          │
                    │        │          middleware: [PolicyMiddleware, AuditMiddleware,     │          │
                    │        │                       MetricsMiddleware, ErrorHandling]      │          │
                    │        │          providers: ProxyProvider(upstream A, ns="a"),       │          │
                    │        │                     ProxyProvider(upstream B, ns="b"), ...   │          │
                    │        │                                                              │          │
                    │        │  PolicySnapshot (in-memory, immutable)  ◄── PolicyWatcher    │          │
                    │        │     effective_tools(principal) → set[str]     (change stream │          │
                    │        │                                              + 30 s poll)    │          │
                    │        └──────────────────────────────────────────────────────────────┘          │
                    │               │ outbound (gateway's own creds)        ▲ watch                     │
                    │               ▼                                       │                           │
                    │   Upstream MCP A (Deployment)   Upstream MCP B   MongoDB replica set (Percona op)  │
                    │                                                       ▲ w:majority writes        │
                    │   admin-api (Node/TS) ◄── admin-ui (Node)  ───────────┘                           │
                    │     RBAC via same IdP (admin role), JSON-schema validation, version bump, history  │
                    └──────────────────────────────────────────────────────────────────────────────────┘
                              IdP (Keycloak / Entra / Okta): JWKS, issuer, audience = gateway resource URI
```

### 3.1 Request flow

1. **ASGI auth layer** (ours, Starlette middleware around the `/mcp` mount only):
   - No `Authorization` header → `Principal(kind="anonymous")`.
   - `Authorization: Bearer <jwt>` → verify with `JWTVerifier` (JWKS from the issuer's OIDC discovery, cached, `kid`-keyed refresh, alg allow-list, `iss`, `aud` = gateway resource URI, `exp`/`nbf`, small skew). Valid → `Principal(kind="user", sub, client_id, scopes, claims)`. Invalid/expired → **401** with `WWW-Authenticate: Bearer resource_metadata="https://mcp.<domain>/.well-known/oauth-protected-resource"`. Never downgrade a bad token to guest. Token in query string or cookie → ignored (401 if that is the only credential).
   - Principal is stored on `request.state` and a ContextVar; FastMCP middleware reads it via `get_http_request()`.
   - Operators can set `GATEWAY_ALLOW_ANONYMOUS=false` to turn the gateway strict (401 for guests).
2. **FastMCP** handles protocol negotiation for both eras. We call `mcp.http_app(path="/mcp", stateless_http=True, host_origin_protection=True, allowed_hosts=[...])`.
3. **PolicyMiddleware** (FastMCP `Middleware`):
   - `on_list_tools`: `allowed = snapshot.effective_tools(principal)`; return only tools whose namespaced name matches. Attach freshness hints `ttlMs` (e.g. 30 000) and `cacheScope: "private"` for modern clients **[verify FastMCP exposes these on the list result]**.
   - `on_call_tool`: if the tool is not allowed → raise `ToolError("Tool not found")` (do not leak existence). This is mandatory: list filtering alone does not block calls.
   - `on_list_resources` / `on_list_resource_templates` / `on_list_prompts`: return `[]`; `on_read_resource` / `on_get_prompt`: not found. (v1 exposes tools only. Policy schema reserves fields for later.)
   - `on_discover` **[verify]** that `server/discover` does not enumerate tools; if it does, filter there too.
4. **ProxyProvider** per upstream forwards the call with the gateway's own upstream credentials (`forward_incoming_headers=False`), per-upstream timeouts, and returns the upstream result unchanged.
5. **AuditMiddleware** emits one structured event per `tools/call` and per denied call: principal kind/sub/client_id, tool, upstream, outcome, latency, trace id. **MetricsMiddleware** updates Prometheus counters/histograms.

### 3.2 Policy data model (MongoDB, database `mcp_gateway`)

All collections carry `$jsonSchema` validators (draft-4 subset), `validationAction: error`. Writes from the admin API use `w: majority`; gateway reloads use `readConcern: majority`.

**`upstreams`** — the proxied servers
```json
{
  "_id": "github",                      // also the tool namespace prefix → tools appear as "github_<tool>"
  "displayName": "GitHub MCP",
  "enabled": true,
  "transport": "http",                  // http | sse | stdio (stdio = local dev only; rejected when GATEWAY_ENV=prod)
  "url": "http://mcp-github.mcp-upstreams.svc:8080/mcp",
  "auth": { "type": "static_bearer", "secretRef": "upstream-github" },   // none | static_bearer | static_header | client_credentials(v2)
  "sessionMode": "shared",              // shared (stateless upstream) | per_request
  "timeoutsMs": { "connect": 2000, "call": 60000 },
  "cacheTtlSec": 300,
  "tags": ["external"],
  "updatedAt": "...", "updatedBy": "..."
}
```
Secrets are **not** stored in Mongo: `secretRef` names a Kubernetes Secret key mounted into the gateway (`/etc/gateway/upstream-secrets/<secretRef>`), which ESO/Vault populates.

**`toolsets`** — named allow-lists (the "whitelists")
```json
{
  "_id": "public-basics",
  "description": "Open to everyone",
  "enabled": true,
  "allow": ["time_now", "docs_search", "weather_*"],   // namespaced tool names; fnmatch globs allowed
  "deny":  [],                                          // explicit deny wins over allow within the union
  "updatedAt": "...", "updatedBy": "..."
}
```

**`grants`** — who gets which toolsets
```json
{
  "_id": "anon-default",
  "enabled": true,
  "priority": 100,
  "subject": { "kind": "anonymous" },                   // anonymous | authenticated | claim | client_id
  "toolsets": ["public-basics"]
}
{
  "_id": "developers",
  "enabled": true,
  "priority": 50,
  "subject": { "kind": "claim", "path": "realm_access.roles", "op": "contains", "value": "mcp-developer" },
  "toolsets": ["public-basics", "engineering"]
}
{
  "_id": "ci-bot",
  "subject": { "kind": "client_id", "value": "ci-runner" },
  "toolsets": ["ci-tools"]
}
```
Evaluation (deny-by-default): `effective = ∪ allow(toolsets of all matching grants) − ∪ deny(same toolsets)`. `anonymous` grants match **everyone** (guest set is the floor); `authenticated` matches any valid token; `claim` supports `op ∈ {eq, in, contains, exists}` with a dotted JSON path (covers Keycloak `realm_access.roles`, Entra `roles`/`scp`, Okta `groups`). `priority` only orders display/audit; it does not change set semantics.

**`meta`** — `{ "_id": "policy", "version": 42, "updatedAt": ... }`. Every admin write bumps `version` in the same transaction. Gateways poll this document every 30 s as a fallback and compare to the version of their in-memory snapshot.

**`policy_history`** — append-only copies of each changed document with `actor`, `at`, `before`, `after` (admin audit). TTL index optional.

### 3.3 Hot reload

`PolicyWatcher` (one per gateway process):
- On startup: load `upstreams`, `toolsets`, `grants`, `meta.version` → build `PolicySnapshot` (immutable; pre-compiled matchers; `effective_tools()` memoised per principal "key" = kind + sorted matching grant ids).
- Open one change stream over the database filtered to those collections (`$match` on `ns.coll`), `fullDocument: updateLookup`. Any event → debounce 250 ms → full reload → atomic swap of the snapshot → `notifications/tools/list_changed` to this pod's `subscriptions/listen` subscribers **[verify FastMCP API for emitting list_changed in the modern era; fall back to clients' `ttlMs` expiry]**.
- Keep the last resume token in memory; on error resume; on `ChangeStreamHistoryLost` or any failure, reload fully and reopen. Watcher health is a readiness signal with grace (pod stays ready on a stale-but-loaded snapshot; `/readyz` reports `policy_age_seconds`).
- Poll `meta.version` every 30 s regardless (covers missed events and Atlas tiers that cap change streams).
- Upstream changes: new/enabled upstreams are mounted live (`mcp.mount(create_proxy(...), namespace=id)`); disabled/removed upstreams are hidden by policy immediately and physically unmounted on next rolling restart (FastMCP has no documented unmount **[verify]**; if a Provider removal API exists, do it live).

### 3.4 Upstream connectivity

- Prod upstreams are HTTP services (separate Deployments in an `mcp-upstreams` namespace or sidecars). A `stdio` server that must be used in prod is wrapped once with a 20-line FastMCP shim image that exposes it over streamable HTTP; stdio from inside the gateway pod is disabled when `GATEWAY_ENV=prod`.
- Gateway → upstream auth: static bearer/header from Secret (v1); OAuth client-credentials via `ClientCredentialsOAuthProvider` or RFC 8693 token exchange carrying the end-user `sub` (v2).
- Optional identity propagation (v2): `X-Gateway-Principal` signed JWT (gateway-issued, short-lived) so upstreams can do their own per-user logic without receiving the client's token.
- Session mode: `shared` keeps one connected `Client` per upstream per process (modern/stateless upstreams); `per_request` uses FastMCP's default `client_factory` for legacy stateful upstreams.
- Health: background probe per upstream every 30 s (`list_tools` with short timeout); exposes `gateway_upstream_up{upstream}` and marks the upstream degraded (tools still listed from cache, calls fail fast with a clear error).

### 3.5 Admin API + UI (Node)

- `admin-api` (TypeScript, Fastify, `mongodb` driver): REST CRUD for `upstreams`, `toolsets`, `grants`; validates request bodies with the same JSON Schema files that generate the Mongo validators; wraps each mutation + `meta.version` bump + `policy_history` insert in a transaction (`w: majority`). Auth: same IdP, requires role `mcp-admin`. Endpoints: `GET/PUT/DELETE /api/{upstreams|toolsets|grants}/:id`, `GET /api/catalog` (proxied from gateway `/internal/catalog`: all upstream tools with name, description, namespace, so the UI offers a picker instead of free text), `POST /api/preview` (body: a principal description or a JWT; returns the effective tool list by calling gateway `/internal/effective-tools`), `GET /api/history`.
- `admin-ui` (Next.js or Vite+React): pages for upstreams, toolsets (tool picker with glob preview), grants (claim-matcher builder), live "effective tools for…" preview, history. The user may already have a frontend; the API contract above is the integration point.
- Gateway `/internal/*` routes are reachable only from the admin-api pod (NetworkPolicy) and require a service token header; they never bypass to `/mcp`.

### 3.6 Deployment (OpenShift)

- **Image**: `registry.access.redhat.com/ubi9/python-312-minimal`, deps via `uv` lockfile, numeric `USER 1001`, group-0 writable app dir, `readOnlyRootFilesystem: true` with `emptyDir` on `/tmp`, listens on 8080 (MCP) and 9090 (metrics). SBOM + Trivy/Grype scan in CI.
- **Process model**: one uvicorn worker per pod (so one change stream and one upstream client pool per pod); scale with replicas, not workers. Resource requests/limits set, memory limit = request.
- **Helm chart** `deploy/helm/mcp-gateway`: Deployment (3 replicas, `terminationGracePeriodSeconds: 60`, SIGTERM closes SSE listeners gracefully), Service, Route (edge or re-encrypt TLS, `haproxy.router.openshift.io/timeout: 1h`, `disable_cookies: "true"`), HPA (CPU + custom metric `gateway_open_listen_streams` via the custom-metrics adapter, optional), PDB (`minAvailable: 2`), NetworkPolicy (ingress from `openshift-ingress` and admin-api only; egress to Mongo, upstreams, IdP JWKS, OTel collector), ServiceAccount with `automountServiceAccountToken: false`, ConfigMap (non-secret config), ExternalSecret → Secret (Mongo URI, upstream creds, internal service token), ServiceMonitor, startup/readiness/liveness probes on `/healthz` + `/readyz`.
- **SSE**: emit `:` keep-alive comments every 15 s on `subscriptions/listen` streams **[verify FastMCP does this; otherwise wrap]**, set `X-Accel-Buffering: no`.
- **MongoDB**: prod = Percona Operator for MongoDB (`PerconaServerMongoDB` CR, replica set, `sharding.enabled: false`, 3 members, backups to object storage, TLS, SCRAM users: `gateway` read-only, `admin-api` read-write). Alternatives: MCK (if Enterprise entitlement) or Atlas M10+. Dev/test = single-node replica set container.
- **Secrets**: External Secrets Operator for OpenShift → Vault/cloud secret manager. Nothing secret in Helm values or Mongo.
- **CI/CD**: GitHub Actions or OpenShift Pipelines (Tekton) for build/test/scan/push; OpenShift GitOps (Argo CD) applies the Helm chart per environment. Image tags are immutable digests.
- **Observability**: FastMCP native OTel tracing exported via OTLP (`FASTMCP_TELEMETRY_MODE=native`); JSON logs to stdout; Prometheus metrics: `gateway_requests_total{method,era,principal_kind,status}`, `gateway_tool_calls_total{tool,upstream,principal_kind,outcome}`, `gateway_tool_call_duration_seconds`, `gateway_policy_denials_total{tool,principal_kind}`, `gateway_policy_version`, `gateway_policy_age_seconds`, `gateway_change_stream_up`, `gateway_upstream_up{upstream}`, `gateway_upstream_errors_total`, `gateway_open_listen_streams`. Alerts: policy age > 5 min, change stream down > 2 min, upstream down, 5xx ratio, denial spike.
- **Security hardening**: Origin/Host validation, max body size (1 MiB default), `ResponseLimitingMiddleware` for oversized tool results, per-pod `RateLimitingMiddleware` keyed by principal (global limits via Redis are v2), no token passthrough, audit log retained 90 days, dependency pinning, non-root, no stdio in prod.

### 3.7 Local development (Podman on macOS)

- `brew install podman && podman machine init && podman machine start` (Podman 6: libkrun provider default on Apple Silicon). `podman compose` uses the already-installed `docker-compose` v2 as provider. Docker Desktop 29 is present and works unchanged as an interim; choose Podman if the Docker licence is a concern for the organisation.
- `local/compose.yaml`: `mongo:8` single-node replica set (keyfile, `rs.initiate` init script, port 27017, URI `mongodb://…/?replicaSet=rs0&directConnection=true`), `keycloak` dev mode with an imported realm (`mcp` realm, audience mapper for `mcp-gateway`, roles `mcp-developer`, `mcp-admin`, test users, a client-credentials client `ci-runner`), two sample upstream FastMCP servers (`sample-public`: time/echo; `sample-private`: a "secret" tool), `admin-api`, `admin-ui`, and the gateway with hot reload. `make up`, `make seed` (loads example upstreams/toolsets/grants), `make smoke` (runs an MCP client as guest and as a developer and asserts the tool lists differ).

## 4. Repository layout

```
mcp-gateway/
├─ gateway/                       # Python 3.12, FastMCP 4.1, uv
│  ├─ src/mcp_gateway/
│  │  ├─ app.py                   # Starlette assembly, routes, lifespan
│  │  ├─ settings.py              # pydantic-settings (GATEWAY_* env)
│  │  ├─ auth/{jwt.py,principal.py,asgi.py,metadata.py}
│  │  ├─ policy/{models.py,store.py,snapshot.py,matcher.py,watcher.py,middleware.py}
│  │  ├─ upstreams/{models.py,registry.py,proxy.py,health.py}
│  │  ├─ middleware/{audit.py,metrics.py}
│  │  ├─ internal_routes.py       # /internal/catalog, /internal/effective-tools
│  │  ├─ health.py                # /healthz /readyz
│  │  └─ telemetry.py
│  ├─ tests/{unit,integration}    # pytest; integration uses testcontainers (mongo RS) + mock JWKS
│  ├─ Containerfile
│  └─ pyproject.toml
├─ admin-api/                     # TypeScript, Fastify, mongodb, ajv
├─ admin-ui/                      # TypeScript, React
├─ schemas/                       # JSON Schema (source of truth) → Mongo validators + ajv + pydantic
├─ sample-upstreams/              # two tiny FastMCP servers used by local + e2e
├─ deploy/
│  ├─ helm/mcp-gateway/           # chart + values-{dev,staging,prod}.yaml
│  ├─ helm/mcp-admin/
│  └─ openshift/                  # namespace, Percona CR, ExternalSecrets, NetworkPolicies, Argo app
├─ local/                         # compose.yaml, mongo-init/, keycloak/realm.json, Makefile targets
└─ docs/                          # this plan, ADRs, runbook
```

## 5. Shared interfaces (fix these first so work packages can run in parallel)

```python
# gateway/src/mcp_gateway/auth/principal.py
@dataclass(frozen=True)
class Principal:
    kind: Literal["anonymous", "user"]
    sub: str | None = None
    client_id: str | None = None
    scopes: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)
    @property
    def cache_key(self) -> str: ...     # stable key for memoising effective tools

# gateway/src/mcp_gateway/policy/snapshot.py
class PolicySnapshot:                   # immutable
    version: int
    loaded_at: datetime
    upstreams: Mapping[str, UpstreamConfig]
    def effective_tools(self, p: Principal) -> ToolFilter: ...   # ToolFilter.allows(namespaced_name) -> bool
    def matching_grants(self, p: Principal) -> list[str]: ...

# gateway/src/mcp_gateway/policy/store.py
class PolicyStore(Protocol):
    async def load(self) -> PolicySnapshot: ...
    def watch(self) -> AsyncIterator[ChangeEvent]: ...          # change stream + poll merged

# gateway/src/mcp_gateway/upstreams/registry.py
class UpstreamRegistry:
    async def apply(self, snapshot: PolicySnapshot) -> None: ...  # mount new/enabled upstreams
    async def catalog(self) -> list[CatalogTool]: ...             # for /internal/catalog
    def health(self) -> Mapping[str, UpstreamHealth]: ...
```

`schemas/*.schema.json` are the single source of truth for `upstreams`, `toolsets`, `grants`; a script generates the Mongo validators (`local/mongo-init/validators.js`), and both pydantic (gateway) and ajv (admin-api) load the files directly.

## 6. Work packages

| WP | Scope | Depends on | Size | Done when |
|---|---|---|---|---|
| **WP0 Scaffold + local infra** | Repo layout, `schemas/`, `local/compose.yaml` (Mongo RS, Keycloak realm, sample upstreams), Makefile, pre-commit, CI skeleton. Install Podman. | – | S | `make up` brings everything up; `make seed` loads example policy; Keycloak issues a token with `aud=mcp-gateway`. |
| **WP1 Gateway core** | FastMCP 4.1 app, settings, `http_app(stateless_http=True)`, health/metrics routes, static mount of the two sample upstreams, Containerfile, unit test harness. | WP0 | M | Guest MCP client lists sample tools through the gateway; image runs as UID 1001 with read-only root FS. |
| **WP2 Auth layer** | `Principal`, JWKS-backed `JWTVerifier` wiring with OIDC discovery, ASGI middleware (anonymous / valid / 401), RFC 9728 metadata route, `GATEWAY_ALLOW_ANONYMOUS`, tests with a mock JWKS and with Keycloak. | WP1 (interfaces from §5) | M | Tests cover: no header, valid, expired, wrong aud, wrong iss, alg=none, token in query string. |
| **WP3 Policy store + middleware** | Mongo models, validators, `PolicySnapshot`, matcher (globs, claim paths, union/deny), `PolicyWatcher` (change stream + poll + resume), `PolicyMiddleware` (list/call/resources/prompts), `list_changed` emission, metrics for policy version/age. | WP1, §5 | L | Editing a `toolsets` doc changes `tools/list` on all replicas within 2 s; denied `tools/call` returns "not found"; chaos test: Mongo step-down resumes the stream. |
| **WP4 Upstream registry** | Upstreams from Mongo, `create_proxy` with `forward_incoming_headers=False`, Secret-backed creds, session modes, timeouts, cache TTL, health probes, live mount of new upstreams, `/internal/catalog`, stdio guard in prod. | WP1, §5 | M | Adding an upstream in Mongo exposes its tools (once granted); killing an upstream degrades only its tools. |
| **WP5 Admin API + UI** | Fastify API with ajv validation, transactions, version bump, history, catalog/preview proxies, admin-role auth; React UI for the three collections + preview. | WP0 schemas; WP3/WP4 for live preview | L | An admin edits a grant in the UI and the preview and a real MCP client reflect it. |
| **WP6 OpenShift packaging** | Helm charts, Route/HPA/PDB/NetworkPolicy/ServiceAccount/ExternalSecret/ServiceMonitor, Percona `PerconaServerMongoDB` CR, Argo CD app, pipeline (build, test, scan, push by digest). | WP1 | M | `helm template` passes `kube-score`/`conftest` restricted-SCC checks; deploys to a dev cluster with 3 replicas; SSE stream survives > 30 s through the Route. |
| **WP7 Hardening + ops** | OTel export, audit events, rate/response limiting, load test (k6: list + call mix, guest vs user), failure drills (Mongo failover, upstream down, IdP JWKS outage with cached keys), runbook + alerts, ADRs. | WP2–WP6 | M | SLO doc: p95 `tools/list` < 150 ms cached, < 600 ms cold; zero 5xx during rolling restart under load. |

Parallelisation: WP0 → WP1 → {WP2, WP3, WP4, WP6} in parallel (four workers) → WP5 (can start on schemas right after WP0; its live preview waits for WP3/WP4) → WP7. Each WP is one worker session with its own branch and PR; WP1 defines the §5 interfaces as stubs so the others compile against them.

## 7. Decisions taken (change here if you disagree)

1. **Python + FastMCP 4.1**, single process per pod, `stateless_http=True`; legacy-era clients are served without sessions and must re-list tools (no push).
2. **Optional auth is implemented outside FastMCP** (ASGI middleware); FastMCP's `require_scopes`/`AuthMiddleware` are not used because they assume a mandatory `AuthProvider`.
3. **Anonymous grants are the floor for everyone**; authenticated callers get the union of guest + role toolsets. Deny lists win.
4. **Invalid/expired tokens are a hard 401**, never downgraded to guest.
5. **Client tokens are never forwarded upstream**; upstream credentials belong to the gateway (Secrets via ESO).
6. **Tools only in v1**; resources and prompts are hidden.
7. **Change stream + 30 s version poll** per pod; no Redis, no shared session store.
8. **Percona Operator** for prod Mongo; single-node replica set container locally; **Podman** (Docker Desktop acceptable interim).
9. **Helm chart rendered by Argo CD**; Routes (edge/re-encrypt) rather than Gateway API unless the platform already runs OSSM 3.
10. Upstream additions are live; removals take effect via policy immediately and physically on the next rolling restart.

## 8. Open questions for you

1. Which IdP (Keycloak, Entra ID, Okta)? Determines the default claim path for roles and the local Keycloak realm can mirror it.
2. Should **authenticated** users always inherit the guest toolset (plan says yes)?
3. Do you already have the Node admin UI/service, or should WP5 build both? If you have one, share its API shape and WP5 becomes an adapter.
4. Prod Mongo: Percona operator in-cluster, or Atlas / an existing platform Mongo?
5. Do upstreams need the end-user identity (token exchange / signed principal header)? Plan defers to v2.
6. Local: Podman now, or keep Docker Desktop until the gateway works?

## 9. Risks

- **FastMCP API churn**: 4.x is two months old; pin `fastmcp==4.1.*`, keep the policy/auth layers independent of FastMCP internals (only `Middleware`, `create_proxy`, `mount`, `http_app`).
- **Unverified FastMCP details** (flagged **[verify]** above): `ttlMs`/`cacheScope` on list results, emitting `list_changed` to `subscriptions/listen` streams, SSE keep-alives, unmounting providers, `on_discover` content. WP1/WP3 must resolve these in their first day and record findings in `docs/adr/`.
- **Legacy clients** get no push notifications; acceptable because every list call is re-evaluated, but document it.
- **Per-request proxy sessions** against legacy stateful upstreams cost an `initialize` per call; prefer modern/stateless upstreams or `sessionMode: shared`.
- **Change-stream limits** on small Atlas tiers; the version poll keeps correctness, only latency degrades.
- **Guest discoverability**: clients that only learn about auth from a 401 will not realise elevated tools exist; the RFC 9728 metadata is still published and the strict mode exists for deployments that want 401s.
