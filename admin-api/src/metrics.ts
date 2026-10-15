import { Counter, collectDefaultMetrics, Gauge, Histogram, Registry } from "prom-client";

export interface Metrics {
  registry: Registry;
  requests: Counter<"method" | "route" | "status">;
  duration: Histogram<"method" | "route">;
  writes: Counter<"collection" | "op">;
  authFailures: Counter<"reason">;
  gatewayErrors: Counter<"route">;
  policyVersion: Gauge;
}

export function createMetrics(): Metrics {
  const registry = new Registry();
  collectDefaultMetrics({ register: registry, prefix: "admin_api_" });
  return {
    registry,
    requests: new Counter({
      name: "admin_api_requests_total",
      help: "HTTP requests by route template and status code",
      labelNames: ["method", "route", "status"],
      registers: [registry],
    }),
    duration: new Histogram({
      name: "admin_api_request_duration_seconds",
      help: "HTTP request latency by route template",
      labelNames: ["method", "route"],
      buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
      registers: [registry],
    }),
    writes: new Counter({
      name: "admin_api_policy_writes_total",
      help: "Committed policy writes (create/update/delete/noop) per collection",
      labelNames: ["collection", "op"],
      registers: [registry],
    }),
    authFailures: new Counter({
      name: "admin_api_auth_failures_total",
      help: "Rejected API calls by reason (invalid_request, invalid_token, insufficient_role)",
      labelNames: ["reason"],
      registers: [registry],
    }),
    gatewayErrors: new Counter({
      name: "admin_api_gateway_errors_total",
      help: "Failed calls to the gateway's /internal routes",
      labelNames: ["route"],
      registers: [registry],
    }),
    policyVersion: new Gauge({
      name: "admin_api_policy_version",
      help: "meta.version after the last write made by this process",
      registers: [registry],
    }),
  };
}
