/**
 * Proxies to the gateway's /internal routes (catalog, upstream health/reload, effective
 * tools). The gateway is the only component that knows the mounted tools and evaluates the
 * policy, so the preview is computed there, never re-implemented here.
 */
import type { FastifyInstance } from "fastify";
import { decodeJwt } from "jose";
import type { AppDeps } from "../app.js";
import { HttpError } from "../db.js";
import { base64url } from "../gateway.js";

type PreviewBody = { kind: "anonymous" } | { claims: Record<string, unknown> } | { token: string };

const previewSchema = {
  type: "object",
  oneOf: [
    {
      type: "object",
      required: ["kind"],
      additionalProperties: false,
      properties: { kind: { enum: ["anonymous"] } },
    },
    {
      type: "object",
      required: ["claims"],
      additionalProperties: false,
      properties: { claims: { type: "object" } },
    },
    {
      type: "object",
      required: ["token"],
      additionalProperties: false,
      properties: { token: { type: "string", minLength: 10, maxLength: 16384 } },
    },
  ],
} as const;

/**
 * Claims from a JWT WITHOUT verifying it. Only for the preview ("what would this token
 * see?"): the gateway verifies real tokens, and the result is labelled unverified.
 */
export function unverifiedClaims(token: string): Record<string, unknown> {
  try {
    return decodeJwt(token.replace(/^Bearer\s+/i, "").trim()) as Record<string, unknown>;
  } catch (err) {
    throw new HttpError(400, { error: `token is not a decodable JWT: ${(err as Error).message}` });
  }
}

export function registerProxyRoutes(app: FastifyInstance, deps: AppDeps): void {
  const { gateway } = deps;

  async function relay(route: string, call: () => Promise<{ status: number; body: unknown }>) {
    try {
      const { status, body } = await call();
      if (status >= 400)
        throw new HttpError(status >= 500 ? 502 : status, {
          error: "gateway error",
          gateway: body,
        });
      return body;
    } catch (err) {
      deps.metrics?.gatewayErrors.inc({ route });
      throw err;
    }
  }

  app.get("/api/catalog", async () => relay("catalog", () => gateway.get("/internal/catalog")));
  app.get("/api/upstreams/health", async () =>
    relay("health", () => gateway.get("/internal/upstreams/health")),
  );
  app.post("/api/upstreams/reload", async (request) => {
    const body = await relay("reload", () => gateway.post("/internal/upstreams/reload"));
    request.log.info(
      { audit: { op: "upstreams_reload", actor: request.principal?.sub } },
      "upstream reload",
    );
    return body;
  });

  app.post<{ Body: PreviewBody }>(
    "/api/preview",
    { schema: { body: previewSchema } },
    async (request) => {
      const body = request.body;
      let query: Record<string, string>;
      let input: { mode: "anonymous" | "claims" | "token"; verified: boolean; note?: string };
      let claims: Record<string, unknown> | undefined;
      if ("kind" in body) {
        query = { kind: "anonymous" };
        input = { mode: "anonymous", verified: false };
      } else {
        claims = "claims" in body ? body.claims : unverifiedClaims(body.token);
        query = { claims: base64url(claims) };
        input =
          "token" in body
            ? {
                mode: "token",
                verified: false,
                note: "Token decoded WITHOUT signature, issuer, audience or expiry checks; preview only.",
              }
            : { mode: "claims", verified: false };
      }
      const result = (await relay("effective-tools", () =>
        gateway.get("/internal/effective-tools", query),
      )) as Record<string, unknown>;
      return { input, claims, ...result };
    },
  );
}
