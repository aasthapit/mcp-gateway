# ADR 0007: JWT verification on FastMCP's `JWTVerifier`; RFC 9728 metadata by hand

Status: accepted (WP2, 2026-10-15).

## Questions

1. Can the gateway's optional bearer auth be built on `fastmcp.server.auth.providers.jwt.JWTVerifier`
   without handing FastMCP an `auth=` provider (which would make auth mandatory, PLAN
   section 7.2)? How does it cache JWKS keys, and does it refresh on an unknown `kid`?
2. Can `RemoteAuthProvider.get_well_known_routes()` serve `/.well-known/oauth-protected-resource`
   for us, again without `auth=`?

## Evidence (source: `fastmcp==4.1.0`, `mcp==2.3.0` in the pinned venv)

### `JWTVerifier` (`fastmcp/server/auth/providers/jwt.py`)

- Public entry points: `verify_token(token) -> AccessToken | None` (delegates to
  `load_access_token`). `AccessToken` carries `client_id`, `scopes`, `expires_at`,
  `subject`, `claims`. No reason is returned for a rejection (only debug/info logs).
- JWKS cache: one dict `kid -> PEM` for the whole set, refreshed as a whole by
  `_refresh_jwks_key`, stamped with `_jwks_cache_time`, TTL `_cache_ttl = 3600` s
  (not configurable). Keys without `kid` land under `_default`; JWKS entries whose `kty`
  does not match the configured algorithm's family are skipped (RFC 7517 section 5).
- Refresh on unknown `kid`: **present**. `_get_jwks_key` returns a cached key only if the
  cache is fresh and holds the `kid`; otherwise it takes `_jwks_refresh_lock` and fetches,
  throttled by `jwks_refresh_interval` (default 30 s, set in `__init__`). Two
  consequences we did not want:
  - the throttle counts **failed** fetches, so an IdP hiccup at first use blocks all
    tokens whose key is not cached for the whole interval;
  - once the set is older than 1 h, a `kid` that *is* cached is still refused while the
    throttle is active ("No matching fresh JWKS key; refresh available shortly") instead
    of being served from the stale cache.
- Algorithms: exactly one `algorithm` (default RS256). `HS*` with a `jwks_uri` is refused
  at construction; `none` is not in the supported set. The key family of `algorithm`
  decides how the JWKS PEM is imported.
- Claims: `exp` is checked against `time.time()` with **no leeway**; `nbf` and `iat` are
  **not checked** (joserfc's `jwt.decode` validates the signature only; the
  `JWTClaimsRegistry` is not used). `iss` is compared as string/list equality, `aud` as
  string-or-list membership. Unsupported `crit` headers are rejected.
- Fetching: `httpx2.AsyncClient` per fetch unless an `http_client` is injected (tests inject
  an `ASGITransport` client pointing at an in-process IdP).

### `RemoteAuthProvider` / well-known routes (`fastmcp/server/auth/auth.py`, `mcp/server/auth/routes.py`)

- `RemoteAuthProvider(token_verifier, authorization_servers, base_url, scopes_supported)`
  needs a `TokenVerifier` instance only to copy `required_scopes` / `scopes_supported`.
  `get_well_known_routes()` → `get_routes()` → `create_protected_resource_routes()`.
- `create_protected_resource_routes()` builds `mcp.shared.auth.ProtectedResourceMetadata`
  and registers **one** route at the RFC 9728 section 3.1 path-suffixed location
  (`/.well-known/oauth-protected-resource/mcp` for resource `.../mcp`), with
  `Cache-Control: public, max-age=3600` hard-coded and a CORS wrapper. The root path
  `/.well-known/oauth-protected-resource` (what our 401 challenge advertises and what
  WP1's placeholder served) is not registered.
- Nothing in that path depends on `auth=` being passed to `FastMCP`; the routes are
  plain Starlette `Route`s and could be mounted on the outer app.

## Decisions

1. **`GatewayJWTVerifier(JWTVerifier)`** (`gateway/src/mcp_gateway/auth/jwt.py`) keeps the
   parent's JWKS fetch/cache/lock and `_refresh_jwks_key`, and overrides:
   - `_get_jwks_key`: fresh cache hit → key; else refresh **at most once per
     `GATEWAY_JWKS_REFRESH_FLOOR_SEC` (default 300 s) after a successful refresh** and not
     within 5 s of a failed fetch; while throttled or when the fetch fails, a **stale**
     cached key for the same `kid` is still accepted (an IdP outage never logs out callers
     whose key we already hold). The parent's `jwks_refresh_interval` is set to 0 so its
     own throttle is inert. Random `kid` values therefore cost at most one JWKS fetch per
     5 minutes.
   - `verify(token) -> AccessToken` raising `TokenInvalid(reason)`: header `alg` must be
     in the explicit allow-list (`GATEWAY_JWT_ALGORITHMS`, default `RS256`; settings refuse
     `none`, `HS*` and mixed key families), signature via joserfc with that allow-list,
     `exp` **required** and `nbf` honoured with `GATEWAY_JWT_CLOCK_SKEW_SEC` (default 30 s)
     leeway, `iss` equality, `aud` membership of `GATEWAY_JWT_AUDIENCE`. `load_access_token`
     still returns `None` so the FastMCP `TokenVerifier` protocol holds.
   - Reason codes (`malformed, alg_not_allowed, unsupported_crit, unknown_kid,
     jwks_unavailable, bad_signature, missing_exp, expired, not_yet_valid, wrong_issuer,
     wrong_audience, unsupported_scheme, not_configured`) go to the `auth.rejected` audit
     line only; clients get a generic `error_description`.
2. **JWKS discovery at startup** (`JwtAuth.startup()` in the app lifespan): `jwks_uri` from
   `<GATEWAY_OIDC_ISSUER>/.well-known/openid-configuration`, the document's `issuer` must
   match. `GATEWAY_JWKS_URI` skips discovery (containers where the host-visible issuer URL
   is unreachable). Discovery failure is fatal in prod and a warning in dev, where the
   next bearer token retries it (30 s apart). Readiness probe `oidc` reports the state;
   it is critical only in prod.
3. **ASGI placement**: `BearerAuthMiddleware` (`auth/asgi.py`) is passed through
   `FastMCP.http_app(middleware=[...])`, so it wraps only the FastMCP sub-app mounted at
   `/` (i.e. `/mcp`), after FastMCP's host/origin guard; `/healthz`, `/readyz`, `/metrics`
   and the well-known routes live on the outer Starlette app and never see it. Absent
   header → anonymous (401 when `GATEWAY_ALLOW_ANONYMOUS=false`); any present credential
   that is not a valid Bearer JWT → 401 JSON `{"error":"invalid_token"}` with
   `WWW-Authenticate: Bearer resource_metadata="<public_base_url>/.well-known/oauth-protected-resource", error="invalid_token"`;
   query-string and cookie tokens are never read. The principal is stored on
   `scope["state"]["principal"]` and bound to `principal_var` for the request.
4. **RFC 9728 metadata is hand-registered** (`auth/metadata.py`): the document is built
   with the SDK's `ProtectedResourceMetadata` model (same schema `RemoteAuthProvider`
   would emit) and served on **both** `/.well-known/oauth-protected-resource` and
   `/.well-known/oauth-protected-resource/mcp`, `Cache-Control: public, max-age=300`,
   permissive CORS for GET/OPTIONS. Reusing `RemoteAuthProvider` would have meant
   constructing a second verifier object just to satisfy its signature and still adding
   the root path ourselves.
5. **Dev-only `gateway_whoami` tool** (`debug_tools.py`) is registered only when
   `GATEWAY_ENV=dev`; `make smoke` and the integration tests use it to print the principal.

## Verified behaviour

Unit tests (`tests/unit/test_auth_jwt.py`, `test_auth_asgi.py`, in-process IdP with a
generated RSA key set) cover: no header, valid token, expired (inside/outside skew), `nbf`
in the future, wrong `aud`/`iss`, `alg=none`, HS256 signed with the RSA public key as
secret, unknown `kid` → refresh → success, unknown `kid` floored at 5 min, JWKS outage
keeps known keys and backs off, token only in query string / cookie, Basic scheme,
`GATEWAY_ALLOW_ANONYMOUS=false`, unguarded non-MCP routes, metadata on both paths.
Integration tests against the local Keycloak: dev1 password grant → `realm_access.roles`
contains `mcp-developer`; `ci-runner` client credentials → `client_id=ci-runner`;
`other-api` client (added to `local/keycloak/realm.json`) → 401 wrong audience.
