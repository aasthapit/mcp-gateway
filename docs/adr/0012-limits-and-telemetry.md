# 0012 Limits, telemetry and graceful shutdown (WP7)

Status: accepted, 2026-10-15. Code: `gateway/src/mcp_gateway/{telemetry,http_guards,main}.py`,
`middleware/{limits,tracing}.py`. Tests: `tests/unit/test_{limits,telemetry,shutdown}.py`,
`tests/drills/`.

## Context

PLAN 3.6 asks for OTLP traces, per-principal rate limiting, response and body size limits,
graceful SIGTERM handling, and alerts. FastMCP 4.1 provides native OpenTelemetry spans and
the `RateLimitingMiddleware` and `ResponseLimitingMiddleware` middleware, but it does not
provide body limits, client-IP resolution, metrics for these features, or a shutdown
sequence. The gateway runs as N stateless pods with no shared store (PLAN decision 7).

## Decisions

### Telemetry

- `GATEWAY_TELEMETRY_MODE` (`off` default, `propagation_only`, `native`) sets
  `fastmcp.settings.telemetry_mode` directly. The old `os.environ.setdefault` did nothing,
  because FastMCP reads its settings at import time and defaults to `native`. `native`
  installs an SDK `TracerProvider` with a `BatchSpanProcessor` and an OTLP exporter:
  http/protobuf (`<endpoint>/v1/traces`) or grpc, selected by `GATEWAY_OTLP_PROTOCOL`.
  The endpoint is `GATEWAY_OTLP_ENDPOINT`, then `OTEL_EXPORTER_OTLP_ENDPOINT`, then
  localhost.
- Resource attributes: `service.name` (`GATEWAY_OTEL_SERVICE_NAME`), `service.version`,
  `deployment.environment.name` and `service.instance.id`. The k8s pod, namespace and node
  come from the Downward API env (`POD_NAME`, `POD_NAMESPACE`, `NODE_NAME`), and anything in
  `OTEL_RESOURCE_ATTRIBUTES` is merged in.
- FastMCP opens one SERVER span per request at its middleware seam, before our middleware
  runs. `SpanAttributesMiddleware` (outermost) adds `gateway.principal.kind`,
  `gateway.principal.client_id`, `gateway.policy.version` and, on `tools/call`,
  `gateway.upstream`. `sub` is left off spans on purpose: the audit log has it, and traces
  often have wider access.
- A W3C `traceparent` on the HTTP request is continued (`HttpGuardMiddleware` attaches it).
  FastMCP alone only reads `_meta`. Outbound calls to upstreams carry the context in `_meta`
  (FastMCP's proxy client).
- Logs: a structlog processor adds `trace_id` and `span_id` to every line written inside a
  span.
- Verified end to end with `make traces`: an in-process gateway exports to the local
  collector (debug exporter), and the check asserts the SERVER span and its attributes.

### Limits (all per pod)

- **Rate limiting:** FastMCP's token-bucket `RateLimitingMiddleware`, subclassed. Keys are
  `user:<sub>` (or `client_id` for tokens without `sub`) and `ip:<client ip>` for anonymous
  callers. Users and anonymous callers have separate rates and bursts (defaults 50/150 and
  20/60 req/s). The bucket table is an LRU of 10 000 keys, so spoofed or rotating IPs cannot
  grow memory. A rejection is the JSON-RPC error `-32000` and counts
  `gateway_rate_limited_total{principal_kind}`. Every MCP request takes a token, including
  `server/discover` and `tools/list`.
- **Client IP:** the TCP peer, unless the peer is in `GATEWAY_TRUSTED_PROXIES`. In that case
  we walk `X-Forwarded-For` right to left and take the first address that is not a trusted
  proxy. Entries further left are written by the client and are never believed. uvicorn's
  own proxy handling is off (`proxy_headers=False`) so there is one source of truth. The
  chart trusts `10.128.0.0/14`, the default OpenShift cluster network where the router pods
  live.
- **Response size:** FastMCP's `ResponseLimitingMiddleware` (1 MB default; an oversized
  result becomes one truncated text block) plus
  `gateway_response_truncated_total{upstream}`. Side effect: it removes `outputSchema` from
  every listed tool, because a truncated result could not match its schema. Clients do not
  get structured-output validation while the limit is on. Set `RESPONSE_MAX_BYTES=0` to
  keep the schemas.
- **Body size:** a pure-ASGI guard around the whole app. If `Content-Length` is above
  `GATEWAY_MAX_REQUEST_BODY_BYTES` (1 MiB) the request gets 413 before the app runs. A
  chunked body is buffered up to the limit and replayed; over the limit it also gets 413.
  The guard counts `gateway_request_body_rejected_total`.
- **HTTP outcome metric:** `gateway_http_responses_total{route,code}`, the input of the
  5xx-ratio alert.

**Why per pod:** a global limit needs a shared counter (Redis or similar) on every request,
and the design deliberately has no shared runtime store. The router spreads load evenly
across stateless replicas, so the effective limit is the configured rate × the replica
count. The runbook says so, and the HPA changes it. Global and per-tenant quotas are v2;
the key function (`rate_limit_key`) is the seam for them.

### Graceful shutdown

`main.GatewayServer` (a subclass of `uvicorn.Server`) calls `Gateway.begin_drain()` before
uvicorn's own shutdown. That call sets readiness to draining (`/readyz` 503) and closes
every `subscriptions/listen` stream with its final `SubscriptionsListenResult`, the
spec's graceful close, so clients re-listen on another pod. uvicorn then stops accepting
connections and waits up to `GATEWAY_SHUTDOWN_GRACE_SEC` (45 s) for in-flight requests,
and only then runs the lifespan shutdown.

**Found while testing:** sse-starlette patches `uvicorn.Server.handle_exit` and **ends every
SSE response the moment SIGTERM arrives**. That cut in-flight legacy-era `tools/call`
answered over SSE (the client sees `peer closed connection`). `build_gateway()` disables
its automatic drain (`AppStatus.disable_automatic_graceful_drain()`). After the grace
period, uvicorn cancels whatever is still running.

`tests/unit/test_shutdown.py` proves the sequence: a listen stream ends gracefully before a
slow modern (JSON) call and a slow legacy (SSE) call finish, both return results, and the
server exits. The test fails without the sse-starlette fix. uvicorn re-raises the captured
SIGTERM after a clean shutdown, so the exit status is 143, not 0.

### JWKS during an IdP outage

FastMCP fetches JWKS with a 10 s timeout, under a lock, inline with the request. With the
IdP blackholed, once the 1 h cache expired every request queued for up to 10 s. Now:

- the fetch is bounded by `GATEWAY_JWKS_FETCH_TIMEOUT_SEC` (3 s);
- a token whose `kid` is cached is verified with the stale key immediately while one
  background task revalidates the set (stale-while-revalidate, with backoff after a
  failure);
- only an unknown `kid` waits for a fetch, at most once per backoff window, and then gets 401.

The `test_idp_down` drill measured this through a cut TCP proxy in front of Keycloak:
cached-key requests stayed under 30 ms, and an unknown `kid` got 401 in under 2 s
(blackhole) or a few ms (refused).

### Alerts

Alerts are a `PrometheusRule` in the gateway chart (enabled in prod), unit-tested with
`promtool test rules` in `make deploy-check`. Policy freshness uses a new gauge,
`gateway_policy_staleness_seconds`: the time since the store last confirmed the snapshot.
`gateway_policy_age_seconds` grows while nobody edits the policy, so "age > 5 min" would
fire on any quiet afternoon.

## Consequences

- With the default limits, a large NAT of guests behind one IP shares one bucket per pod.
  Operators raise `RATE_LIMIT_ANONYMOUS_*` or ask those users to authenticate.
- `outputSchema` disappears from tool listings while the response limit is on. This is
  documented in the gateway README, and `RESPONSE_MAX_BYTES=0` restores it.
- Rollouts need `terminationGracePeriodSeconds ≥ preStop + SHUTDOWN_GRACE_SEC + ~5 s`
  (chart: 5 + 45 < 60). Tool calls longer than the grace period are cut during a rollout.
- The `sigterm-first` rolling-restart drill shows the router's connection-failure retries
  matter: without the preStop pause, about 1 % of requests needed a retry to the other pod.
  The OpenShift router (HAProxy) retries failed connections, and the preStop sleep makes
  it unnecessary.
