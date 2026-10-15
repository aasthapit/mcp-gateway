# admin-api

TypeScript / Fastify 5 admin API over the gateway policy in MongoDB (`upstreams`,
`toolsets`, `grants`, `meta`, `policy_history`). Request bodies are validated with ajv
against the JSON Schemas in [`../schemas`](../schemas), the same files that generate the
Mongo validators and that the gateway's pydantic models mirror. Design and auth model:
[ADR 0011](../docs/adr/0011-admin-api.md).

## Run

```bash
make up && make seed          # repo root: Mongo, Keycloak, sample upstreams, seed policy
make admin-dev                # API :3001 + UI :3000 with hot reload (+ gateway :8080)
```

or only the API:

```bash
cd admin-api
npm ci
ADMIN_GATEWAY_INTERNAL_TOKEN=dev-internal-token-change-me npm run dev
curl -s localhost:3001/readyz
curl -s -H "Authorization: Bearer $(make -s -C .. token-admin)" localhost:3001/api/meta
```

Container: `podman build -f admin-api/Containerfile .` from the repo root (the image
carries `schemas/`), or `make admin-up` (compose profile `admin`). UBI 9
`nodejs-22-minimal`, UID 1001, read-only root filesystem.

## Endpoints

All `/api/*` routes need `Authorization: Bearer <jwt>` with `aud` containing
`ADMIN_JWT_AUDIENCE` and the role `ADMIN_REQUIRED_ROLE` (401 without/invalid token, 403
without the role). Documents are returned as stored (`updatedAt` ISO string, `updatedBy` =
the writer's `sub`).

| Method + path                  | What                                                                                                                                                                                                                                                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/{collection}`        | `{items, version}`; collection = `upstreams`, `toolsets` or `grants`                                                                                                                                                                                                                                          |
| `GET /api/{collection}/:id`    | one document, 404 if missing                                                                                                                                                                                                                                                                                  |
| `PUT /api/{collection}/:id`    | create or replace (full document; `_id` from the URL, `updatedAt`/`updatedBy` ignored). 400 with `details: [{path, message}]` on schema errors. Returns `{op: create\|update\|noop, version, document, warnings}`                                                                                             |
| `DELETE /api/{collection}/:id` | `{op: delete, version, document}` (the deleted one); 404 if missing; **409** `{referencedBy: {grants\|toolsets: [...]}}` for a toolset named by a grant or an upstream named by a toolset pattern (`<upstream>_…`)                                                                                            |
| `GET /api/meta`                | `{_id: "policy", version, updatedAt, updatedBy}`                                                                                                                                                                                                                                                              |
| `GET /api/history`             | `?collection=&id=&limit=` (default 50, max 500), newest first: `{items: [{collection, id, op, actor, actorName, at, version, before, after}]}`                                                                                                                                                                |
| `GET /api/catalog`             | proxy of gateway `GET /internal/catalog`: `{upstreams, tools: [{namespaced_name, upstream, name, description, input_schema}]}`                                                                                                                                                                                |
| `GET /api/upstreams/health`    | proxy of `GET /internal/upstreams/health`                                                                                                                                                                                                                                                                     |
| `POST /api/upstreams/reload`   | proxy of `POST /internal/upstreams/reload`                                                                                                                                                                                                                                                                    |
| `POST /api/preview`            | body `{kind: "anonymous"}`, `{claims: {...}}` or `{token: "<jwt>"}` → gateway `/internal/effective-tools`: `{input, claims, principal, policy_version, tools, grants}`. A token is **decoded without verification** only to read its claims (`input.verified: false`)                                         |
| `GET /healthz`                 | process up (no auth)                                                                                                                                                                                                                                                                                          |
| `GET /readyz`                  | Mongo ping, 503 when it fails (no auth)                                                                                                                                                                                                                                                                       |
| `GET /metrics`                 | Prometheus text (no auth): `admin_api_requests_total{method,route,status}`, `admin_api_request_duration_seconds`, `admin_api_policy_writes_total{collection,op}`, `admin_api_auth_failures_total{reason}`, `admin_api_gateway_errors_total{route}`, `admin_api_policy_version`, Node defaults (`admin_api_*`) |

Gateway proxy errors: 503 when `ADMIN_GATEWAY_INTERNAL_TOKEN` is unset, 502 when the
gateway is unreachable or rejects the token.

Every mutation is one transaction with `writeConcern: majority`: the document, `$inc
meta.version` (+ `updatedAt`/`updatedBy`) and a `policy_history` entry. A PUT with
unchanged content is a no-op (no version bump, no history). Upstream ids `health` and
`reload` are reserved (they collide with the proxy routes).

Cross-cutting: pino JSON logs (one line per request with `request_id`; writes log an
`audit` object; `Authorization` redacted), `X-Request-Id` honoured or generated and echoed,
CORS only for `ADMIN_CORS_ORIGINS`, helmet headers (`default-src 'none'`), 1 MiB body
limit, `Cache-Control: no-store`.

## Environment

| Variable                       | Default                                                                        | Notes                                                                     |
| ------------------------------ | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| `ADMIN_ENV`                    | `dev`                                                                          | `prod` requires an https issuer, `ADMIN_MONGO_URI` and the gateway token  |
| `ADMIN_HOST` / `ADMIN_PORT`    | `0.0.0.0` / `3001`                                                             |                                                                           |
| `ADMIN_MONGO_URI`              | `mongodb://root:devpass@127.0.0.1:27017/?replicaSet=rs0&directConnection=true` | needs a replica set (transactions); `readWrite` on the policy db          |
| `ADMIN_MONGO_DB`               | `mcp_gateway`                                                                  |                                                                           |
| `ADMIN_OIDC_ISSUER`            | `http://localhost:8180/realms/mcp`                                             | expected `iss`; JWKS via `<issuer>/.well-known/openid-configuration`      |
| `ADMIN_JWKS_URL`               | –                                                                              | skip discovery (containers: issuer URL not reachable)                     |
| `ADMIN_JWT_AUDIENCE`           | `mcp-admin`                                                                    | required `aud`                                                            |
| `ADMIN_JWT_ALGORITHMS`         | `RS256`                                                                        | CSV; `none`/`HS*` refused                                                 |
| `ADMIN_JWT_CLOCK_SKEW_SEC`     | `30`                                                                           |                                                                           |
| `ADMIN_REQUIRED_ROLE`          | `mcp-admin`                                                                    |                                                                           |
| `ADMIN_ROLES_CLAIM`            | `realm_access.roles`                                                           | dotted path; arrays or space-separated strings (`roles`, `groups`, `scp`) |
| `ADMIN_CORS_ORIGINS`           | `http://localhost:3000,http://127.0.0.1:3000`                                  | CSV of UI origins                                                         |
| `ADMIN_GATEWAY_INTERNAL_URL`   | `http://127.0.0.1:8080`                                                        | gateway base URL (`/internal/...` is appended)                            |
| `ADMIN_GATEWAY_INTERNAL_TOKEN` | –                                                                              | = the gateway's `GATEWAY_INTERNAL_TOKEN`                                  |
| `ADMIN_GATEWAY_TIMEOUT_MS`     | `10000`                                                                        |                                                                           |
| `ADMIN_SCHEMAS_DIR`            | `../schemas` relative to the package                                           | image: `/opt/app-root/src/schemas`                                        |
| `ADMIN_BODY_LIMIT_BYTES`       | `1048576`                                                                      |                                                                           |
| `ADMIN_TRUST_PROXY`            | `true` in prod                                                                 | honour `X-Forwarded-*` from the router                                    |
| `ADMIN_LOG_LEVEL`              | `info`                                                                         | pino level                                                                |

## Tests

```bash
npm test                  # vitest: unit + live projects
npm run test:unit         # no services needed
npm run typecheck
make smoke                # repo root: also runs the admin check (npm run smoke)
```

- `test/unit`: schema validation (seed docs, branch pruning, every subject kind),
  reference rules, JWT verification with a local JWKS (aud/iss/exp/alg=none/role), the
  Fastify app via `inject` (auth hook, 400/404/413, CORS, request ids, preview modes,
  proxies) with a fake gateway.
- `test/live/transaction.test.ts`: `policyWrite` against the local replica set in a
  throwaway database (atomic version bump + history, no-op, aborted 409, concurrent
  writers). Skips when Mongo is unreachable.
- `test/live/integration.test.ts`: Keycloak tokens (admin1 accepted, dev1 401), a
  temporary anonymous grant + toolset created through the API; editing the toolset must
  change a guest's `tools/list` within 2 s; preview, 409, history, catalog, health.
  Starts its own gateway (`uv run mcp-gateway` on a free port) unless `GATEWAY_URL` and
  `GATEWAY_INTERNAL_TOKEN` point at a running one. Skips when Keycloak is unreachable.
- `scripts/smoke.ts` (`make admin-smoke`, part of `make smoke`): admin1 token → `PUT
/api/toolsets/public-basics` adding `sample-private_secret_lookup` → the guest sees it
  within 2 s → revert → hidden again; preview and history checked.
