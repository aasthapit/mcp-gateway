/**
 * Bearer JWT verification for /api/* (ADR 0011): tokens from the same OIDC issuer as the
 * gateway, verified with jose against the issuer's JWKS (OIDC discovery, or ADMIN_JWKS_URL),
 * `aud` must contain ADMIN_JWT_AUDIENCE (mcp-admin) and the roles claim must contain
 * ADMIN_REQUIRED_ROLE (mcp-admin). Missing/invalid token → 401, valid without the role → 403.
 */
import {
  createRemoteJWKSet,
  errors as joseErrors,
  type JWTPayload,
  type JWTVerifyGetKey,
  jwtVerify,
} from "jose";
import type { FastifyRequest } from "fastify";
import type { Config } from "./config.js";

declare module "fastify" {
  interface FastifyRequest {
    principal?: Principal;
  }
}

export interface Principal {
  sub: string;
  name: string | undefined;
  roles: string[];
  claims: JWTPayload;
}

export class AuthError extends Error {
  constructor(
    readonly status: 401 | 403,
    readonly code: "invalid_request" | "invalid_token" | "insufficient_role",
    message: string,
  ) {
    super(message);
  }
}

export function resolveClaim(claims: Record<string, unknown>, path: string): unknown {
  let cur: unknown = claims;
  for (const part of path.split(".")) {
    if (!cur || typeof cur !== "object" || Array.isArray(cur)) return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

export function rolesFrom(claims: Record<string, unknown>, path: string): string[] {
  const value = resolveClaim(claims, path);
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === "string");
  if (typeof value === "string") return value.split(/\s+/).filter(Boolean);
  return [];
}

export type Verifier = (token: string) => Promise<Principal>;

async function discoverJwks(issuer: string, timeoutMs: number): Promise<URL> {
  const res = await fetch(`${issuer}/.well-known/openid-configuration`, {
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`OIDC discovery for ${issuer}: HTTP ${res.status}`);
  const body = (await res.json()) as { issuer?: string; jwks_uri?: string };
  if (body.issuer && body.issuer.replace(/\/+$/, "") !== issuer) {
    throw new Error(`OIDC discovery issuer mismatch: ${body.issuer} != ${issuer}`);
  }
  if (!body.jwks_uri) throw new Error(`OIDC discovery for ${issuer} has no jwks_uri`);
  return new URL(body.jwks_uri);
}

export function createVerifier(cfg: Config, keyOverride?: JWTVerifyGetKey): Verifier {
  let keys: Promise<JWTVerifyGetKey> | undefined = keyOverride
    ? Promise.resolve(keyOverride)
    : undefined;
  const getKeys = () => {
    keys ??= (
      cfg.jwksUrl ? Promise.resolve(new URL(cfg.jwksUrl)) : discoverJwks(cfg.oidcIssuer, 5_000)
    ).then((url) => createRemoteJWKSet(url, { cooldownDuration: 30_000, cacheMaxAge: 600_000 }));
    // A failed discovery is retried on the next request instead of being cached forever.
    keys.catch(() => {
      keys = undefined;
    });
    return keys;
  };

  return async (token) => {
    let payload: JWTPayload;
    try {
      ({ payload } = await jwtVerify(token, await getKeys(), {
        issuer: cfg.oidcIssuer,
        audience: cfg.jwtAudience,
        algorithms: cfg.jwtAlgorithms,
        clockTolerance: cfg.jwtClockSkewSec,
        requiredClaims: ["sub", "exp"],
      }));
    } catch (err) {
      if (err instanceof joseErrors.JOSEError) {
        throw new AuthError(401, "invalid_token", `${err.code}: ${err.message}`);
      }
      throw err; // discovery / JWKS fetch failure: a 5xx, not the caller's fault
    }
    const roles = rolesFrom(payload as Record<string, unknown>, cfg.rolesClaim);
    const principal: Principal = {
      sub: String(payload.sub),
      name: typeof payload.preferred_username === "string" ? payload.preferred_username : undefined,
      roles,
      claims: payload,
    };
    if (!roles.includes(cfg.requiredRole)) {
      throw new AuthError(403, "insufficient_role", `role ${cfg.requiredRole} required`);
    }
    return principal;
  };
}

export function bearerToken(header: string | undefined): string {
  if (!header) throw new AuthError(401, "invalid_request", "missing bearer token");
  const m = /^Bearer\s+([A-Za-z0-9._~+/-]+=*)\s*$/i.exec(header);
  if (!m?.[1]) throw new AuthError(401, "invalid_request", "Authorization must be 'Bearer <jwt>'");
  return m[1];
}

/** The verified caller of an /api route (set by the onRequest hook in app.ts). */
export function principalOf(request: FastifyRequest): Principal {
  if (!request.principal) throw new AuthError(401, "invalid_request", "unauthenticated");
  return request.principal;
}
