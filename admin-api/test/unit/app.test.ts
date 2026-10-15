import type { FastifyInstance } from "fastify";
import type { Db, MongoClient } from "mongodb";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { buildApp } from "../../src/app.js";
import { AuthError, type Principal } from "../../src/auth.js";
import { loadConfig } from "../../src/config.js";
import type { GatewayClient } from "../../src/gateway.js";
import { buildValidators } from "../../src/schemas.js";

// No Mongo here: every request below is answered before the database is touched.
const config = loadConfig({ ADMIN_BODY_LIMIT_BYTES: "2048" });
const calls: { path: string; query?: Record<string, string> }[] = [];
const gateway: GatewayClient = {
  get: async (path, query) => {
    calls.push({ path, query });
    return {
      status: 200,
      body: { tools: ["sample-public_echo"], grants: ["anon-default"], policy_version: 7 },
    };
  },
  post: async (path) => ({ status: 200, body: { reloaded: path } }),
};
const admin: Principal = { sub: "u-1", name: "admin1", roles: ["mcp-admin"], claims: {} };
const verifier = async (token: string) => {
  if (token === "admin.token.x") return admin;
  if (token === "dev.token.x")
    throw new AuthError(403, "insufficient_role", "role mcp-admin required");
  throw new AuthError(401, "invalid_token", "bad token");
};
const mongo = {
  client: {} as MongoClient,
  db: { command: async () => ({ ok: 1 }) } as unknown as Db,
};
let app: FastifyInstance;

beforeAll(async () => {
  app = await buildApp({
    config,
    mongo,
    verifier,
    gateway,
    validators: buildValidators(config.schemasDir),
    logger: false,
  });
});
afterAll(() => app.close());

const auth = (t = "admin.token.x") => ({ authorization: `Bearer ${t}` });

describe("auth hook", () => {
  it("401 with a challenge without a token, 401 invalid_token, 403 without the role", async () => {
    let r = await app.inject({ method: "GET", url: "/api/toolsets" });
    expect(r.statusCode).toBe(401);
    expect(r.headers["www-authenticate"]).toBe('Bearer realm="mcp-admin"');
    r = await app.inject({ method: "GET", url: "/api/toolsets", headers: auth("garbage.token.x") });
    expect(r.statusCode).toBe(401);
    expect(r.headers["www-authenticate"]).toContain('error="invalid_token"');
    r = await app.inject({ method: "GET", url: "/api/catalog", headers: auth("dev.token.x") });
    expect(r.statusCode).toBe(403);
    expect(r.headers["www-authenticate"]).toContain('error="insufficient_scope"');
  });

  it("leaves health and metrics open", async () => {
    expect((await app.inject({ method: "GET", url: "/healthz" })).statusCode).toBe(200);
    expect((await app.inject({ method: "GET", url: "/readyz" })).json()).toMatchObject({
      status: "ready",
    });
    const m = await app.inject({ method: "GET", url: "/metrics" });
    expect(m.body).toContain("admin_api_requests_total");
    expect(m.body).toContain('admin_api_auth_failures_total{reason="insufficient_role"} 1');
  });
});

describe("request handling", () => {
  it("validates PUT bodies against the schema before any write", async () => {
    const r = await app.inject({
      method: "PUT",
      url: "/api/grants/g1",
      headers: auth(),
      payload: { enabled: true, subject: { kind: "claim", path: "roles" }, toolsets: ["a"] },
    });
    expect(r.statusCode).toBe(400);
    expect(r.json().details).toContainEqual({
      path: "/subject",
      message: "must have required property 'op'",
    });
  });

  it("rejects unknown collections, bad ids, reserved upstream ids and mismatched _id", async () => {
    const put = (url: string, payload: object) =>
      app.inject({ method: "PUT", url, headers: auth(), payload });
    expect(
      (await app.inject({ method: "GET", url: "/api/users/x", headers: auth() })).statusCode,
    ).toBe(404);
    expect((await put("/api/toolsets/Bad_Id", { enabled: true, allow: [] })).statusCode).toBe(400);
    expect((await put("/api/upstreams/health", {})).statusCode).toBe(400);
    expect((await put("/api/toolsets/a", { _id: "b", enabled: true, allow: [] })).statusCode).toBe(
      400,
    );
  });

  it("enforces the body limit", async () => {
    const r = await app.inject({
      method: "PUT",
      url: "/api/toolsets/big",
      headers: { ...auth(), "content-type": "application/json" },
      payload: JSON.stringify({ enabled: true, allow: [], description: "x".repeat(4096) }),
    });
    expect(r.statusCode).toBe(413);
  });

  it("echoes a sane X-Request-Id and replaces a hostile one", async () => {
    let r = await app.inject({
      method: "GET",
      url: "/healthz",
      headers: { "x-request-id": "abc-123" },
    });
    expect(r.headers["x-request-id"]).toBe("abc-123");
    r = await app.inject({ method: "GET", url: "/healthz", headers: { "x-request-id": "a b\nc" } });
    expect(r.headers["x-request-id"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("sets security headers and CORS only for the UI origin", async () => {
    let r = await app.inject({
      method: "OPTIONS",
      url: "/api/toolsets/x",
      headers: { origin: "http://localhost:3000", "access-control-request-method": "PUT" },
    });
    expect(r.headers["access-control-allow-origin"]).toBe("http://localhost:3000");
    r = await app.inject({
      method: "OPTIONS",
      url: "/api/toolsets/x",
      headers: { origin: "https://evil.test", "access-control-request-method": "PUT" },
    });
    expect(r.headers["access-control-allow-origin"]).toBeUndefined();
    r = await app.inject({ method: "GET", url: "/healthz" });
    expect(r.headers["x-content-type-options"]).toBe("nosniff");
    expect(r.headers["content-security-policy"]).toContain("default-src 'none'");
  });
});

describe("gateway proxies", () => {
  it("preview: anonymous, claims (base64url) and an unverified token", async () => {
    calls.length = 0;
    const post = (payload: object) =>
      app.inject({ method: "POST", url: "/api/preview", headers: auth(), payload });
    let r = await post({ kind: "anonymous" });
    expect(r.json()).toMatchObject({ input: { mode: "anonymous" }, tools: ["sample-public_echo"] });
    expect(calls.at(-1)).toEqual({
      path: "/internal/effective-tools",
      query: { kind: "anonymous" },
    });

    const claims = { sub: "x", realm_access: { roles: ["mcp-developer"] } };
    r = await post({ claims });
    expect(r.json().input.mode).toBe("claims");
    const sent = calls.at(-1)?.query?.claims ?? "";
    expect(JSON.parse(Buffer.from(sent, "base64url").toString())).toEqual(claims);

    const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
    r = await post({ token: `${enc({ alg: "RS256" })}.${enc(claims)}.c2ln` });
    expect(r.json()).toMatchObject({ input: { mode: "token", verified: false }, claims });
    expect(r.json().input.note).toMatch(/WITHOUT signature/);

    expect((await post({ token: "not-a-jwt-at-all" })).statusCode).toBe(400);
    expect((await post({ kind: "user" })).statusCode).toBe(400);
    expect((await post({ kind: "anonymous", claims: {} })).statusCode).toBe(400);
  });

  it("catalog, health and reload are relayed", async () => {
    expect(
      (await app.inject({ method: "GET", url: "/api/catalog", headers: auth() })).statusCode,
    ).toBe(200);
    expect(calls.at(-1)?.path).toBe("/internal/catalog");
    expect(
      (await app.inject({ method: "GET", url: "/api/upstreams/health", headers: auth() }))
        .statusCode,
    ).toBe(200);
    expect(calls.at(-1)?.path).toBe("/internal/upstreams/health");
    const r = await app.inject({ method: "POST", url: "/api/upstreams/reload", headers: auth() });
    expect(r.json()).toEqual({ reloaded: "/internal/upstreams/reload" });
  });
});
