/**
 * End to end against the local stack (`make up && make seed`): Keycloak tokens, the admin
 * API in-process, and a gateway (GATEWAY_URL + GATEWAY_INTERNAL_TOKEN, or a fresh
 * `uv run mcp-gateway`). A throwaway anonymous grant + toolset are created through the
 * API; editing the toolset must change the guest's tools/list within 2 s. Everything it
 * creates is deleted again. Skips when Mongo or Keycloak are unreachable.
 */
import { randomBytes } from "node:crypto";
import type { FastifyInstance } from "fastify";
import type { Db, MongoClient } from "mongodb";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { buildApp } from "../../src/app.js";
import { createVerifier } from "../../src/auth.js";
import { loadConfig } from "../../src/config.js";
import { connectMongo } from "../../src/db.js";
import { createGatewayClient } from "../../src/gateway.js";
import { buildValidators } from "../../src/schemas.js";
import {
  ISSUER,
  MONGO_URI,
  PRIVATE_TOOL,
  password,
  reachable,
  type RunningGateway,
  startGateway,
  waitForGuest,
} from "../../scripts/stack.js";

const live =
  (await reachable(`${ISSUER}/.well-known/openid-configuration`)) &&
  process.env.ADMIN_SKIP_LIVE !== "1";

describe.skipIf(!live)("admin API against the local stack", () => {
  const id = `it-${randomBytes(3).toString("hex")}`;
  let gw: RunningGateway;
  let app: FastifyInstance;
  let mongo: { client: MongoClient; db: Db };
  let admin: string;
  const call = (
    method: "GET" | "PUT" | "POST" | "DELETE",
    url: string,
    payload?: object,
    token = admin,
  ) =>
    app.inject({
      method,
      url,
      headers: { authorization: `Bearer ${token}` },
      ...(payload ? { payload } : {}),
    });

  beforeAll(async () => {
    gw = await startGateway();
    const config = loadConfig(process.env, {
      oidcIssuer: ISSUER,
      mongoUri: MONGO_URI,
      gatewayUrl: gw.url,
      gatewayToken: gw.token,
    });
    mongo = await connectMongo(config.mongoUri, config.mongoDb);
    app = await buildApp({
      config,
      mongo,
      validators: buildValidators(config.schemasDir),
      verifier: createVerifier(config),
      gateway: createGatewayClient(config.gatewayUrl, config.gatewayToken, config.gatewayTimeoutMs),
      logger: false,
    });
    admin = await password("admin1");
  });

  afterAll(async () => {
    if (app) {
      await call("DELETE", `/api/grants/${id}`);
      await call("DELETE", `/api/toolsets/${id}`);
      await app.close();
    }
    await mongo?.client.close();
    await gw?.stop();
  });

  it("rejects dev1 (no aud=mcp-admin) and accepts admin1", async () => {
    const dev1 = await call("GET", "/api/meta", undefined, await password("dev1"));
    expect(dev1.statusCode).toBe(401);
    const meta = await call("GET", "/api/meta");
    expect(meta.statusCode).toBe(200);
    expect(meta.json().version).toBeGreaterThan(0);
  });

  it("a toolset edit through the API changes the guest's tools/list within 2 s", async () => {
    let r = await call("PUT", `/api/toolsets/${id}`, {
      enabled: true,
      allow: [],
      description: "admin-api integration test",
    });
    expect(r.statusCode).toBe(200);
    r = await call("PUT", `/api/grants/${id}`, {
      enabled: true,
      subject: { kind: "anonymous" },
      toolsets: [id],
    });
    expect(r.json()).toMatchObject({ op: "create", warnings: [] });
    expect((await waitForGuest(gw.url, (n) => !n.has(PRIVATE_TOOL), "baseline")) >= 0).toBe(true);

    r = await call("PUT", `/api/toolsets/${id}`, {
      enabled: true,
      allow: [PRIVATE_TOOL],
      description: "admin-api integration test",
    });
    expect(r.json().op).toBe("update");
    const v1 = r.json().version as number;
    const took = await waitForGuest(gw.url, (n) => n.has(PRIVATE_TOOL), "toolset edit");
    expect(took).toBeLessThanOrEqual(2);

    const preview = await call("POST", "/api/preview", { kind: "anonymous" });
    expect(preview.json().tools).toContain(PRIVATE_TOOL);
    expect(preview.json().grants).toContain(id);

    const blocked = await call("DELETE", `/api/toolsets/${id}`);
    expect(blocked.statusCode).toBe(409);
    expect(blocked.json().referencedBy).toEqual({ grants: [id] });

    r = await call("DELETE", `/api/grants/${id}`);
    expect(r.json().version).toBeGreaterThan(v1);
    await waitForGuest(gw.url, (n) => !n.has(PRIVATE_TOOL), "grant delete");
    expect((await call("DELETE", `/api/toolsets/${id}`)).statusCode).toBe(200);

    const history = (await call("GET", `/api/history?id=${id}&limit=10`)).json().items as {
      op: string;
      collection: string;
    }[];
    expect(history.map((h) => `${h.collection}:${h.op}`)).toEqual([
      "toolsets:delete",
      "grants:delete",
      "toolsets:update",
      "grants:create",
      "toolsets:create",
    ]);
  });

  it("catalog and upstream health come from the gateway", async () => {
    const catalog = (await call("GET", "/api/catalog")).json();
    expect(catalog.tools.map((t: { namespaced_name: string }) => t.namespaced_name)).toContain(
      PRIVATE_TOOL,
    );
    const health = (await call("GET", "/api/upstreams/health")).json();
    expect(Object.keys(health.upstreams)).toEqual(
      expect.arrayContaining(["sample-public", "sample-private"]),
    );
  });
});
