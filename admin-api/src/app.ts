/**
 * Fastify assembly: security headers, CORS, request ids, metrics, auth hook and routes.
 * `buildApp` takes its collaborators so tests can inject a key resolver, a Mongo handle
 * or a fake gateway; `server.ts` wires the real ones.
 */
import { randomUUID } from "node:crypto";
import cors from "@fastify/cors";
import helmet from "@fastify/helmet";
import Fastify, { type FastifyInstance, LogController } from "fastify";
import type { Db, MongoClient } from "mongodb";
import { AuthError, bearerToken, type Verifier } from "./auth.js";
import type { Config } from "./config.js";
import { HttpError } from "./db.js";
import type { GatewayClient } from "./gateway.js";
import { createMetrics, type Metrics } from "./metrics.js";
import { registerPolicyRoutes } from "./routes/policy.js";
import { registerProxyRoutes } from "./routes/proxy.js";
import type { Validators } from "./schemas.js";

export interface AppDeps {
  config: Config;
  mongo: { client: MongoClient; db: Db };
  verifier: Verifier;
  gateway: GatewayClient;
  validators: Validators;
  metrics?: Metrics;
  logger?: boolean | Record<string, unknown>;
}

const REQUEST_ID = /^[A-Za-z0-9._:-]{1,128}$/;

export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  const { config } = deps;
  const metrics = deps.metrics ?? createMetrics();
  const app = Fastify({
    logger: deps.logger ?? {
      level: config.logLevel,
      redact: ["req.headers.authorization", "req.headers.cookie"],
    },
    bodyLimit: config.bodyLimitBytes,
    // Route schemas reject unknown fields instead of silently dropping them.
    ajv: { customOptions: { removeAdditional: false, coerceTypes: false } },
    trustProxy: config.trustProxy,
    logController: new LogController({ requestIdLogLabel: "request_id" }),
    genReqId: (req) => {
      const incoming = req.headers["x-request-id"];
      return typeof incoming === "string" && REQUEST_ID.test(incoming) ? incoming : randomUUID();
    },
  });

  await app.register(helmet, {
    // JSON only: nothing may be framed, scripted or embedded. same-site lets the UI on
    // another localhost port read responses (CORS still decides who may).
    contentSecurityPolicy: {
      useDefaults: false,
      directives: { defaultSrc: ["'none'"], frameAncestors: ["'none'"] },
    },
    crossOriginResourcePolicy: { policy: "same-site" },
  });
  await app.register(cors, {
    origin: config.corsOrigins,
    methods: ["GET", "PUT", "POST", "DELETE"],
    allowedHeaders: ["Authorization", "Content-Type", "X-Request-Id"],
    exposedHeaders: ["X-Request-Id"],
    maxAge: 600,
  });

  app.addHook("onSend", async (request, reply) => {
    reply.header("x-request-id", request.id);
    reply.header("cache-control", "no-store");
  });
  app.addHook("onResponse", async (request, reply) => {
    const route = request.routeOptions.url ?? "unmatched";
    metrics.requests.inc({ method: request.method, route, status: String(reply.statusCode) });
    metrics.duration.observe({ method: request.method, route }, reply.elapsedTime / 1000);
  });

  // Everything under /api needs an admin token; health and metrics stay open (the Route
  // only exposes /api, NetworkPolicy keeps the rest in-cluster).
  app.addHook("onRequest", async (request) => {
    if (request.method === "OPTIONS" || !request.url.startsWith("/api/")) return;
    try {
      request.principal = await deps.verifier(bearerToken(request.headers.authorization));
    } catch (err) {
      if (err instanceof AuthError) metrics.authFailures.inc({ reason: err.code });
      throw err;
    }
  });

  app.setErrorHandler((err, request, reply) => {
    if (err instanceof AuthError) {
      const challenge =
        err.code === "invalid_request"
          ? `Bearer realm="mcp-admin"`
          : `Bearer realm="mcp-admin", error="${err.code === "insufficient_role" ? "insufficient_scope" : err.code}"`;
      return reply
        .code(err.status)
        .header("www-authenticate", challenge)
        .send({ error: err.message, code: err.code });
    }
    if (err instanceof HttpError) {
      if (err.status >= 500) request.log.warn({ err: err.body }, "upstream dependency failed");
      return reply.code(err.status).send(err.body);
    }
    const e = err as { validation?: unknown; statusCode?: number; message: string };
    if (e.validation) return reply.code(400).send({ error: "invalid request", details: e.message });
    if (e.statusCode && e.statusCode >= 400 && e.statusCode < 500) {
      return reply.code(e.statusCode).send({ error: e.message });
    }
    request.log.error({ err }, "unhandled error");
    return reply.code(500).send({ error: "internal error", request_id: request.id });
  });
  app.setNotFoundHandler((request, reply) =>
    reply.code(404).send({ error: `no route ${request.method} ${request.url}` }),
  );

  app.get("/healthz", async () => ({ status: "ok" }));
  app.get("/readyz", async (_request, reply) => {
    try {
      await deps.mongo.db.command({ ping: 1 }, { timeoutMS: 2_000 });
      return { status: "ready", checks: { mongo: "ok" } };
    } catch (err) {
      return reply
        .code(503)
        .send({ status: "not ready", checks: { mongo: (err as Error).message } });
    }
  });
  app.get("/metrics", async (_request, reply) => {
    reply.header("content-type", metrics.registry.contentType);
    return metrics.registry.metrics();
  });

  registerProxyRoutes(app, deps);
  registerPolicyRoutes(app, { ...deps, metrics });
  return app;
}
