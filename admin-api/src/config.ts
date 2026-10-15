/**
 * Process configuration from `ADMIN_*` environment variables (see README "Environment").
 * Defaults target the local stack (`make up`); `ADMIN_ENV=prod` refuses the dev-only ones.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";

export interface Config {
  env: "dev" | "prod";
  host: string;
  port: number;
  logLevel: string;
  trustProxy: boolean;
  bodyLimitBytes: number;
  mongoUri: string;
  mongoDb: string;
  oidcIssuer: string;
  /** Explicit JWKS endpoint; skips OIDC discovery (containers: issuer is not reachable). */
  jwksUrl: string | undefined;
  jwtAudience: string;
  jwtAlgorithms: string[];
  jwtClockSkewSec: number;
  requiredRole: string;
  /** Dotted claim path holding the caller's roles (Keycloak: realm_access.roles). */
  rolesClaim: string;
  corsOrigins: string[];
  gatewayUrl: string;
  gatewayToken: string | undefined;
  gatewayTimeoutMs: number;
  schemasDir: string;
}

const here = path.dirname(fileURLToPath(import.meta.url));

function csv(value: string | undefined, fallback: string[]): string[] {
  if (value === undefined || value.trim() === "") return fallback;
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function int(name: string, value: string | undefined, fallback: number): number {
  if (value === undefined || value === "") return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < 0)
    throw new Error(`${name} must be a non-negative integer, got ${value}`);
  return n;
}

function bool(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

export function loadConfig(
  env: NodeJS.ProcessEnv = process.env,
  overrides: Partial<Config> = {},
): Config {
  const mode = (env.ADMIN_ENV ?? "dev") === "prod" ? "prod" : "dev";
  const cfg: Config = {
    env: mode,
    host: env.ADMIN_HOST ?? "0.0.0.0",
    port: int("ADMIN_PORT", env.ADMIN_PORT ?? env.PORT, 3001),
    logLevel: env.ADMIN_LOG_LEVEL ?? "info",
    trustProxy: bool(env.ADMIN_TRUST_PROXY, mode === "prod"),
    bodyLimitBytes: int("ADMIN_BODY_LIMIT_BYTES", env.ADMIN_BODY_LIMIT_BYTES, 1024 * 1024),
    // 127.0.0.1, not localhost: the podman port forward is IPv4-only on macOS.
    mongoUri:
      env.ADMIN_MONGO_URI ??
      "mongodb://root:devpass@127.0.0.1:27017/?replicaSet=rs0&directConnection=true",
    mongoDb: env.ADMIN_MONGO_DB ?? "mcp_gateway",
    oidcIssuer: (env.ADMIN_OIDC_ISSUER ?? "http://localhost:8180/realms/mcp").replace(/\/+$/, ""),
    jwksUrl: env.ADMIN_JWKS_URL || undefined,
    jwtAudience: env.ADMIN_JWT_AUDIENCE ?? "mcp-admin",
    jwtAlgorithms: csv(env.ADMIN_JWT_ALGORITHMS, ["RS256"]),
    jwtClockSkewSec: int("ADMIN_JWT_CLOCK_SKEW_SEC", env.ADMIN_JWT_CLOCK_SKEW_SEC, 30),
    requiredRole: env.ADMIN_REQUIRED_ROLE ?? "mcp-admin",
    rolesClaim: env.ADMIN_ROLES_CLAIM ?? "realm_access.roles",
    corsOrigins: csv(env.ADMIN_CORS_ORIGINS, ["http://localhost:3000", "http://127.0.0.1:3000"]),
    gatewayUrl: (env.ADMIN_GATEWAY_INTERNAL_URL ?? "http://127.0.0.1:8080").replace(/\/+$/, ""),
    gatewayToken: env.ADMIN_GATEWAY_INTERNAL_TOKEN || undefined,
    gatewayTimeoutMs: int("ADMIN_GATEWAY_TIMEOUT_MS", env.ADMIN_GATEWAY_TIMEOUT_MS, 10_000),
    // src/ and dist/ both sit one level below the package; the schemas live next to it.
    schemasDir: env.ADMIN_SCHEMAS_DIR ?? path.resolve(here, "../../schemas"),
    ...overrides,
  };
  if (cfg.env === "prod") {
    if (!cfg.oidcIssuer.startsWith("https://"))
      throw new Error("ADMIN_OIDC_ISSUER must be https in prod");
    if (!cfg.gatewayToken) throw new Error("ADMIN_GATEWAY_INTERNAL_TOKEN is required in prod");
    if (!env.ADMIN_MONGO_URI) throw new Error("ADMIN_MONGO_URI is required in prod");
  }
  const bad = cfg.jwtAlgorithms.filter((a) => a === "none" || a.startsWith("HS"));
  if (bad.length) throw new Error(`ADMIN_JWT_ALGORITHMS must be asymmetric, got ${bad.join(",")}`);
  return cfg;
}
