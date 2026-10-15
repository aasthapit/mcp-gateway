/**
 * Helpers for the live checks against the local stack (`make up && make seed`): the
 * integration test (test/live/integration.test.ts) and `npm run smoke` (make smoke).
 * Dev tooling only; not part of the server build.
 */
import { type ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const repoRoot = path.resolve(here, "../..");

export const KEYCLOAK =
  process.env.ADMIN_IT_KEYCLOAK_URL ?? `http://localhost:${process.env.KEYCLOAK_PORT ?? "8180"}`;
export const ISSUER = `${KEYCLOAK}/realms/mcp`;
export const MONGO_URI =
  process.env.ADMIN_MONGO_URI ??
  "mongodb://root:devpass@127.0.0.1:27017/?replicaSet=rs0&directConnection=true";
export const PRIVATE_TOOL = "sample-private_secret_lookup";
/** Dev-only tools the gateway adds in GATEWAY_ENV=dev; not part of the policy. */
const DEBUG_TOOLS = new Set(["gateway_whoami"]);

export async function reachable(url: string, timeoutMs = 1_500): Promise<boolean> {
  try {
    await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    return true;
  } catch {
    return false;
  }
}

export async function password(username: string, clientId = "mcp-admin-ui"): Promise<string> {
  const res = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    body: new URLSearchParams({
      client_id: clientId,
      grant_type: "password",
      username,
      password: username,
    }),
  });
  if (!res.ok)
    throw new Error(
      `token for ${username} via ${clientId}: HTTP ${res.status} ${await res.text()}`,
    );
  return ((await res.json()) as { access_token: string }).access_token;
}

/** Tool names a guest sees, via one stateless modern-era tools/list POST. */
export async function guestTools(gatewayUrl: string): Promise<Set<string>> {
  const res = await fetch(`${gatewayUrl}/mcp`, {
    method: "POST",
    headers: {
      Accept: "application/json, text/event-stream",
      "Content-Type": "application/json",
      "MCP-Protocol-Version": "2026-07-28",
      "Mcp-Method": "tools/list",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/list",
      params: {
        _meta: {
          "io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientCapabilities": {},
        },
      },
    }),
    signal: AbortSignal.timeout(5_000),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`tools/list: HTTP ${res.status} ${text.slice(0, 300)}`);
  // JSON, or an SSE stream whose data: lines carry the JSON-RPC response.
  const json = res.headers.get("content-type")?.includes("text/event-stream")
    ? text
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .find((l) => l.includes('"result"'))
    : text;
  const body = JSON.parse(json ?? "{}") as { result?: { tools?: { name: string }[] } };
  return new Set((body.result?.tools ?? []).map((t) => t.name).filter((n) => !DEBUG_TOOLS.has(n)));
}

/** Poll `guestTools` until `predicate` holds; returns the seconds it took or throws. */
export async function waitForGuest(
  gatewayUrl: string,
  predicate: (names: Set<string>) => boolean,
  what: string,
  budgetSec = 2,
): Promise<number> {
  const start = performance.now();
  for (;;) {
    const names = await guestTools(gatewayUrl);
    const took = (performance.now() - start) / 1000;
    if (predicate(names)) return took;
    if (took > budgetSec)
      throw new Error(
        `${what} not reflected within ${budgetSec}s: ${[...names].sort().join(", ")}`,
      );
    await new Promise((r) => setTimeout(r, 100));
  }
}

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      srv.close(() =>
        typeof addr === "object" && addr ? resolve(addr.port) : reject(new Error("no port")),
      );
    });
  });
}

export interface RunningGateway {
  url: string;
  token: string;
  stop(): Promise<void>;
}

/**
 * A gateway to test against: GATEWAY_URL + GATEWAY_INTERNAL_TOKEN when set, otherwise a
 * fresh `uv run mcp-gateway` from ../gateway on a free port (Mongo policy, local stack).
 */
export async function startGateway(
  log: (line: string) => void = () => {},
): Promise<RunningGateway> {
  const external = process.env.GATEWAY_URL;
  if (external) {
    const token = process.env.GATEWAY_INTERNAL_TOKEN;
    if (!token) throw new Error("GATEWAY_URL is set but GATEWAY_INTERNAL_TOKEN is not");
    return { url: external.replace(/\/+$/, ""), token, stop: async () => {} };
  }
  const port = await freePort();
  const token = randomBytes(16).toString("base64url");
  const url = `http://127.0.0.1:${port}`;
  const child: ChildProcess = spawn("uv", ["run", "--frozen", "mcp-gateway"], {
    cwd: path.join(repoRoot, "gateway"),
    env: {
      ...process.env,
      GATEWAY_PORT: String(port),
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_METRICS_PORT: "0",
      GATEWAY_PUBLIC_BASE_URL: url,
      GATEWAY_INTERNAL_TOKEN: token,
      GATEWAY_POLICY_SOURCE: "mongo",
      GATEWAY_UPSTREAM_PROBE_SEC: "1",
      GATEWAY_LOG_LEVEL: process.env.GATEWAY_LOG_LEVEL ?? "WARNING",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const collect = (chunk: Buffer) => {
    output = (output + chunk.toString()).slice(-4000);
  };
  child.stdout?.on("data", collect);
  child.stderr?.on("data", collect);
  const stop = async () => {
    if (child.exitCode !== null) return;
    child.kill("SIGTERM");
    await new Promise((r) => {
      const t = setTimeout(() => {
        child.kill("SIGKILL");
        r(null);
      }, 10_000);
      child.once("exit", () => {
        clearTimeout(t);
        r(null);
      });
    });
  };
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`gateway exited (${child.exitCode}):\n${output}`);
    try {
      const r = await fetch(`${url}/readyz`, { signal: AbortSignal.timeout(1_000) });
      if (r.ok) {
        log(`in-process gateway on ${url}`);
        return { url, token, stop };
      }
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  await stop();
  throw new Error(`gateway on ${url} not ready after 60s:\n${output}`);
}
