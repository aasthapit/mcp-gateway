# admin-ui

React 19 + Vite admin UI for the MCP gateway policy, talking to [`admin-api`](../admin-api).
Plain CSS (light/dark), no component library; `oidc-client-ts` for login and `diff` for the
history view are the only runtime dependencies.

## Run

```bash
make up && make seed     # repo root
make admin-dev           # UI http://localhost:3000, API :3001, gateway :8080
```

Sign in as **admin1 / admin1** (realm role `mcp-admin`; dev1 can log in to Keycloak but the
API answers 401 because its token has no `aud=mcp-admin`). Then e.g. Grants → `anon-default`
→ tick `engineering` → Save (the form shows the new `meta.version`), and a guest MCP client
on `http://localhost:8080/mcp` lists `sample-private_secret_lookup` within a second:

```bash
cd gateway && uv run python -c 'import asyncio; from fastmcp import Client
async def m():
    async with Client("http://localhost:8080/mcp") as c: print([t.name for t in await c.list_tools()])
asyncio.run(m())'
```

Container: `podman build -f admin-ui/Containerfile .` from the repo root, or `make admin-up`.
The runtime image serves `dist/` with `server.mjs` (Node built-ins only), UID 1001, read-only
root filesystem.

## Pages

| Page      | What                                                                                                                                                                                                                      |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Upstreams | list with live health badges (`/api/upstreams/health`, every 15 s), **Reload gateway** button, form mirroring the schema (enums read from `schemas/upstreams.schema.json`); `secretRef` is a secret _name_, never a value |
| Toolsets  | allow/deny editors with a tool picker fed by `/api/catalog`, fnmatch globs with a live "matches N catalog tools" hint per entry and for the pattern being typed, effective allow − deny summary                           |
| Grants    | subject builder (anonymous / authenticated / claim `{path, op, value}` with typed values / client_id), toolset multi-select                                                                                               |
| Preview   | "effective tools for…" a guest, a claims JSON (templates) or a pasted token (decoded **without verification**, clearly labelled); shows tools, matching grants and the policy version                                     |
| History   | `policy_history` table (filters: collection, id, limit); click a row for the key-sorted before/after JSON diff                                                                                                            |

Every save shows the resulting `meta.version` under the form and in a toast; the header
shows the current version (polled every 15 s). Hash routes: `#/toolsets/<id>`,
`#/grants/new`, …

## Login

Authorization code + PKCE (S256) against the public client `mcp-admin-ui`
(`local/keycloak/realm.json`), tokens in `sessionStorage`, renewal with the refresh token
(no iframes, `monitorSession` off). The access token goes to the API as a bearer token.

## Configuration

Read at runtime from `/config.json`, which the Vite dev server (`vite.config.ts`) and the
container's `server.mjs` build from the environment (`runtime-config.mjs`):

| Variable                   | Default                            | Notes                                                           |
| -------------------------- | ---------------------------------- | --------------------------------------------------------------- |
| `ADMIN_UI_API_BASE_URL`    | `http://localhost:3001`            | `""` = same origin (OpenShift Route `/api` → api)               |
| `ADMIN_UI_OIDC_AUTHORITY`  | `http://localhost:8180/realms/mcp` | issuer URL                                                      |
| `ADMIN_UI_OIDC_CLIENT_ID`  | `mcp-admin-ui`                     | public client with PKCE                                         |
| `ADMIN_UI_OIDC_SCOPE`      | `openid profile`                   |                                                                 |
| `ADMIN_UI_GATEWAY_MCP_URL` | `http://localhost:8080/mcp`        | shown on the Preview page                                       |
| `ADMIN_UI_ENVIRONMENT`     | `local`                            | badge in the header                                             |
| `ADMIN_UI_HOST` / `_PORT`  | `0.0.0.0` / `3000`                 | `server.mjs` (Vite: 127.0.0.1:3000; Keycloak redirects to 3000) |

`server.mjs` sets a CSP whose `connect-src` is limited to the API and IdP origins,
`frame-ancestors 'none'`, `nosniff`, `no-referrer`; `/healthz` for probes; unknown paths
fall back to `index.html`.

## Tests

```bash
npm test          # vitest + testing-library (jsdom)
npm run typecheck
npm run build
```

`test/glob.test.ts` (fnmatch semantics, deny wins), `ToolsetForm` (glob hints, picker,
deny, save payload, API validation errors), `GrantForm` (claim/contains, `in` arrays,
typed numbers, `exists` drops the value, client_id/anonymous), `UpstreamForm` (http with
secretRef name, stdio switch, `toDocument` pruning).
