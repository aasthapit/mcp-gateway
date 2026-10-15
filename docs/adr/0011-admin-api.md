# ADR 0011: admin API (auth model, write path, shared schemas)

Status: accepted (WP5, 2026-10-15). Implements docs/PLAN.md 3.5.

## Context

Admins edit `upstreams`, `toolsets` and `grants` while every gateway replica hot-reloads
them (ADR 0008: change stream + 30 s `meta.version` poll). The admin path must therefore
(a) only let administrators in, (b) never write a document the gateway would skip as
invalid, (c) make every change visible to both reload paths, and (d) leave an audit trail.

## Decision

**Auth model.** `admin-api` (Fastify) accepts bearer JWTs from the same OIDC issuer as the
gateway, verified with `jose` against the issuer's JWKS (OIDC discovery, or
`ADMIN_JWKS_URL` when the issuer URL is not reachable from the pod), RS256 allow-list,
30 s skew, `sub` and `exp` required. Two independent checks:

- `aud` must contain `mcp-admin` (401 otherwise): an access token minted for the gateway
  (`aud=mcp-gateway`) is not an admin credential, even for an admin user;
- the roles claim (`realm_access.roles`, configurable) must contain `mcp-admin` (403).

In Keycloak the audience is not a static mapper on a client (that would give dev1's tokens
`aud=mcp-admin` too) but derived from a role: the client `mcp-admin` has the client role
`policy-admin`, the realm role `mcp-admin` is a composite including it, and Keycloak's
audience-resolve mapper adds `aud=mcp-admin` only for users holding it. The UI logs in with
the public PKCE client `mcp-admin-ui` (authorization code + S256, tokens in
sessionStorage, refresh-token renewal, no iframes). `/healthz`, `/readyz` and `/metrics`
are unauthenticated; the OpenShift Route only exposes `/api` and the UI.

The gateway's `/internal/*` routes (catalog, health, reload, effective tools) are called
with the shared `X-Internal-Token`; the chart reads it from the gateway's own Secret
(`mcp-gateway-env`) so the two cannot drift. The preview's `{token}` mode decodes a JWT
**without** verifying it, only to extract claims, and labels the result `verified: false`:
it answers "what would this token see" and grants nothing.

**Write path.** Every mutation goes through one helper (`admin-api/src/db.ts`
`policyWrite`): one transaction, `readConcern: snapshot`, `writeConcern: majority`,
driver `withTransaction` (retries `TransientTransactionError` / unknown commit result):

1. read `before`; for deletes run the reference guard inside the transaction (a toolset
   named by a grant, an upstream named by a toolset pattern `"<id>_…"` → 409 with the
   referencing ids);
2. `replaceOne` (upsert) the full document with `updatedAt` / `updatedBy = sub`, or
   `deleteOne`;
3. `$inc meta.version` (+ `updatedAt` / `updatedBy`) on `{_id: "policy"}`;
4. insert `{collection, id, op, actor, actorName, at, version, before, after}` into
   `policy_history`.

A PUT whose content equals the stored document is a no-op (no version bump, no history
row). Writes are full-document PUTs (idempotent, no partial-merge semantics to reason
about); concurrent writers each get their own version because the `meta` document is
a write-write conflict that the transaction retry serialises.

**Validation.** Request bodies are validated by ajv (draft-04 mode, `ajv-draft-04`) against
`schemas/*.schema.json`, the same files `schemas/build-mongo-validators.py` turns into the
Mongo `$jsonSchema` validators and the gateway's pydantic models mirror. The API reads
them from `../schemas` at startup (the container image copies them from the repo root),
so there is no generated copy to forget. `updatedAt` / `updatedBy` are server-owned and
stripped from bodies; `_id` comes from the URL. Failed `oneOf` / `anyOf` branches whose
discriminator does not match are pruned so a claim subject without `op` reports exactly
that.

## Consequences

- An edit is visible on every replica within the change-stream debounce (measured
  0.3–0.4 s locally, `make smoke`); with the stream down, within the poll interval.
- Mongo stays the last line of defence (`validationAction: error`), but invalid input is
  rejected with a readable 400 before it reaches the database.
- History grows without bound; a TTL index on `policy_history.at` can be added per site
  (PLAN 3.2 leaves it optional). The reference guard has a write-skew window (a grant
  created concurrently with a toolset delete); the gateway treats a missing toolset as
  empty, so the outcome is safe, only less tidy.
- `GET /api/upstreams/health` shadows an upstream named `health`; ids `health` and `reload`
  are refused for upstreams.
- With the password grant on `mcp-gateway`, admin1's gateway token also carries
  `aud=mcp-admin` (audience resolve applies to every client with full scope). Acceptable
  in the dev realm; a production IdP should scope the `mcp-admin` audience to the admin UI
  client.
