# ADR 0004: SSE keep-alive comments on long-lived streams (verify c)

Status: verified (WP1, 2026-10-15).

## Question

Does FastMCP send SSE keep-alive comments on long-lived streams, so an OpenShift Route
(default idle timeout 30 s) or any proxy does not close an idle `subscriptions/listen`
stream? If not, we would have to wrap the response.

## Evidence

Source:

- Modern era (`mcp/server/_streamable_http_modern.py`): `_SSE_PING_INTERVAL = 15.0`; once a
  response has committed to `text/event-stream` the transport writes `: ping\r\n\r\n` every
  15 s while waiting for the next event. Headers include `cache-control: no-cache,
  no-transform` and `x-accel-buffering: no`.
- Legacy era (`mcp/server/streamable_http.py`, `sse.py`): responses use
  `sse_starlette.EventSourceResponse`, whose `DEFAULT_PING_INTERVAL = 15` emits a ping
  comment every 15 s. In stateless mode the standalone GET stream is disabled, so only
  per-request POST streams exist on the legacy path.

Experiment: a `subscriptions/listen` stream held open for 33 s (see ADR 0003) received
`: ping` at t=16.5 s and t=31.5 s after the last event.

## Decision

No wrapper needed. Keep the Route timeout annotation (`haproxy.router.openshift.io/timeout: 1h`)
from PLAN section 3.6 for long tool calls, and keep uvicorn's `timeout_keep_alive` above
the ping interval (set to 75 s in `main.py`).
