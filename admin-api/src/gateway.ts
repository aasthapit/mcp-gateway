/**
 * Client for the gateway's token-guarded `/internal/*` routes (gateway/README.md
 * "Endpoints"). Every call sends `X-Internal-Token: ADMIN_GATEWAY_INTERNAL_TOKEN`.
 */
import { HttpError } from "./db.js";

export interface GatewayClient {
  get(path: string, query?: Record<string, string>): Promise<{ status: number; body: unknown }>;
  post(path: string): Promise<{ status: number; body: unknown }>;
}

export function base64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj), "utf8").toString("base64url");
}

export function createGatewayClient(
  baseUrl: string,
  token: string | undefined,
  timeoutMs: number,
): GatewayClient {
  async function call(method: "GET" | "POST", path: string, query?: Record<string, string>) {
    if (!token) {
      throw new HttpError(503, {
        error: "gateway proxy disabled: ADMIN_GATEWAY_INTERNAL_TOKEN is not set",
      });
    }
    const url = new URL(baseUrl + path);
    for (const [k, v] of Object.entries(query ?? {})) url.searchParams.set(k, v);
    let res: Response;
    try {
      res = await fetch(url, {
        method,
        headers: { "X-Internal-Token": token, Accept: "application/json" },
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      throw new HttpError(502, {
        error: `gateway unreachable at ${baseUrl}: ${(err as Error).message}`,
      });
    }
    const text = await res.text();
    let body: unknown;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = { error: text.slice(0, 500) };
    }
    if (res.status === 403 || res.status === 404) {
      // 404 = the gateway has no internal token configured; 403 = tokens differ.
      throw new HttpError(502, {
        error: `gateway rejected the internal token (HTTP ${res.status}); check GATEWAY_INTERNAL_TOKEN / ADMIN_GATEWAY_INTERNAL_TOKEN`,
      });
    }
    return { status: res.status, body };
  }
  return {
    get: (path, query) => call("GET", path, query),
    post: (path) => call("POST", path),
  };
}
