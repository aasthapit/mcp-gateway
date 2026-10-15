/**
 * `make smoke` admin check (`npm run smoke`): the admin API in-process against the local
 * stack and a gateway (GATEWAY_URL + GATEWAY_INTERNAL_TOKEN, or a fresh `uv run
 * mcp-gateway`). admin1 token → PUT toolset public-basics (+ the private tool) → the guest's
 * tools/list changes within 2 s → PUT the original back → the tool is gone again.
 */
import type { AddressInfo } from "node:net";
import { buildApp } from "../src/app.js";
import { createVerifier } from "../src/auth.js";
import { loadConfig } from "../src/config.js";
import { connectMongo } from "../src/db.js";
import { createGatewayClient } from "../src/gateway.js";
import { buildValidators } from "../src/schemas.js";
import { ISSUER, MONGO_URI, PRIVATE_TOOL, password, startGateway, waitForGuest } from "./stack.js";

const TOOLSET = "public-basics";
const ok = (msg: string) => console.log(`  \x1b[32mok\x1b[0m  ${msg}`);

async function main(): Promise<void> {
  const gw = await startGateway((l) => console.log(`  info  ${l}`));
  const config = loadConfig(process.env, {
    port: 0,
    host: "127.0.0.1",
    oidcIssuer: ISSUER,
    mongoUri: MONGO_URI,
    gatewayUrl: gw.url,
    gatewayToken: gw.token,
    logLevel: "warn",
  });
  const mongo = await connectMongo(config.mongoUri, config.mongoDb);
  const app = await buildApp({
    config,
    mongo,
    validators: buildValidators(config.schemasDir),
    verifier: createVerifier(config),
    gateway: createGatewayClient(config.gatewayUrl, config.gatewayToken, config.gatewayTimeoutMs),
    logger: false,
  });
  await app.listen({ host: "127.0.0.1", port: 0 });
  const api = `http://127.0.0.1:${(app.server.address() as AddressInfo).port}`;
  let original: Record<string, unknown> | undefined;
  let admin = "";
  const call = async (method: string, path: string, body?: unknown, token = admin) => {
    const res = await fetch(api + path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    return { status: res.status, body: (await res.json()) as Record<string, any> };
  };

  try {
    const ready = await fetch(`${api}/readyz`);
    if (!ready.ok) throw new Error(`admin-api /readyz ${ready.status}`);
    ok(`admin-api in-process on ${api}: /readyz 200 (Mongo ping)`);

    const anon = await fetch(`${api}/api/meta`);
    if (anon.status !== 401) throw new Error(`no token: expected 401, got ${anon.status}`);
    const dev1 = await call("GET", "/api/meta", undefined, await password("dev1"));
    if (dev1.status !== 401 && dev1.status !== 403)
      throw new Error(`dev1: expected 401/403, got ${dev1.status}`);
    ok(`no token -> 401, dev1 (no aud=mcp-admin / no role) -> ${dev1.status}`);

    admin = await password("admin1");
    const got = await call("GET", `/api/toolsets/${TOOLSET}`);
    if (got.status !== 200)
      throw new Error(
        `GET toolset ${TOOLSET}: ${got.status} ${JSON.stringify(got.body)} (make seed?)`,
      );
    const { updatedAt: _a, updatedBy: _b, ...doc } = got.body;
    original = doc;
    const allow = (doc.allow as string[]).filter((t) => t !== PRIVATE_TOOL);

    const put = await call("PUT", `/api/toolsets/${TOOLSET}`, {
      ...doc,
      allow: [...allow, PRIVATE_TOOL],
    });
    if (put.status !== 200) throw new Error(`PUT: ${put.status} ${JSON.stringify(put.body)}`);
    let took = await waitForGuest(gw.url, (n) => n.has(PRIVATE_TOOL), "admin-api toolset edit");
    ok(
      `admin1 PUT /api/toolsets/${TOOLSET} (meta.version ${put.body.version}); guest sees ${PRIVATE_TOOL} after ${took.toFixed(2)}s`,
    );

    const preview = await call("POST", "/api/preview", { kind: "anonymous" });
    if (!(preview.body.tools as string[] | undefined)?.includes(PRIVATE_TOOL)) {
      throw new Error(
        `preview for a guest should include ${PRIVATE_TOOL}: ${JSON.stringify(preview.body)}`,
      );
    }
    ok(
      `POST /api/preview {kind: anonymous} lists ${PRIVATE_TOOL} (grants ${JSON.stringify(preview.body.grants)})`,
    );

    const revert = await call("PUT", `/api/toolsets/${TOOLSET}`, { ...doc, allow });
    if (revert.status !== 200)
      throw new Error(`revert PUT: ${revert.status} ${JSON.stringify(revert.body)}`);
    original = undefined;
    took = await waitForGuest(gw.url, (n) => !n.has(PRIVATE_TOOL), "admin-api toolset revert");
    ok(
      `reverted (meta.version ${revert.body.version}); ${PRIVATE_TOOL} hidden again after ${took.toFixed(2)}s`,
    );

    const history = await call("GET", `/api/history?collection=toolsets&id=${TOOLSET}&limit=2`);
    const entries = history.body.items as { actorName?: string; version: number }[];
    if (entries.length !== 2 || entries.some((e) => e.actorName !== "admin1")) {
      throw new Error(`history should hold the two admin1 writes: ${JSON.stringify(entries)}`);
    }
    ok(
      `policy_history has both writes by admin1 (versions ${entries.map((e) => e.version).join(", ")})`,
    );
  } finally {
    if (original) await call("PUT", `/api/toolsets/${TOOLSET}`, original).catch(() => {});
    await app.close();
    await mongo.client.close();
    await gw.stop();
  }
}

main().catch((err: unknown) => {
  console.error(`  \x1b[31mFAIL\x1b[0m ${(err as Error).message}`);
  process.exit(1);
});
