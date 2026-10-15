# ADR 0003: `notifications/tools/list_changed` in stateless mode (verify b)

Status: verified (WP1, 2026-10-15). Follow-up for WP3.

## Question

Is there an API to push `notifications/tools/list_changed` to clients' `subscriptions/listen`
streams when the gateway runs `stateless_http=True`?

## Evidence

Source:

- `mcp/server/subscriptions.py` ships `SubscriptionBus` (Protocol), `InMemorySubscriptionBus`
  and `ListenHandler`. The SDK's high-level `MCPServer` registers a `ListenHandler` by
  default. FastMCP's `LowLevelServer` subclasses the SDK low-level `Server` and never passes
  `on_subscriptions_listen`; no `listen`/`SubscriptionBus` reference exists anywhere under
  `fastmcp/server`.
- `mcp/server/lowlevel/server.py` `get_capabilities`: on modern versions
  `tools.listChanged` is `True` only when `subscriptions/listen` is served.
- `fastmcp/server/context.py` `send_notification`: on a sessionless connection the
  notification rides the in-flight request's own stream; there is no standing channel.

Experiment (gateway in-process, `stateless_http=True`, raw modern requests):

```
baseline            subscriptions/listen -> 404, {"code": -32601, "message": "Method not found"}
baseline            server/discover      -> capabilities.tools.listChanged == false
with ListenHandler  subscriptions/listen -> 200 text/event-stream
    t=0.0s  notifications/subscriptions/acknowledged  (subscriptionId = request id)
    t=1.5s  notifications/tools/list_changed           (after bus.publish(ToolsListChanged()))
    t=16.5s ": ping"   t=31.5s ": ping"
with ListenHandler  server/discover      -> capabilities.tools.listChanged == true
```

The handler was registered with:

```python
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp_types import SubscriptionsListenRequestParams
bus = InMemorySubscriptionBus()
mcp._mcp_server.add_request_handler("subscriptions/listen", SubscriptionsListenRequestParams, ListenHandler(bus))
...
await bus.publish(ToolsListChanged())   # from the policy watcher after a snapshot swap
```

## Findings

- FastMCP 4.1 itself offers no API for this: out of the box the modern era advertises
  `listChanged: false` and `subscriptions/listen` is not served. Stateless mode also
  disables the legacy GET stream, so legacy clients get no push either (expected).
- The SDK building blocks work unchanged inside FastMCP and in stateless mode. The listen
  request is just a long-lived POST handled by the modern transport; one process-local bus
  fans out to that pod's open streams. Cross-replica fan-out is not needed because every pod
  reloads the policy itself and notifies its own streams.
- `add_request_handler` on `mcp._mcp_server` is a public SDK method but reached through a
  private FastMCP attribute. Re-check on upgrade; a FastMCP release may add
  `subscriptions=` natively.

## Decision

WP3 registers the SDK `ListenHandler` with an `InMemorySubscriptionBus` on the gateway's
low-level server and publishes `ToolsListChanged()` after each snapshot swap. Clients that
do not listen fall back to `ttlMs` expiry (ADR 0002). The per-request filter still applies
to the subsequent `tools/list`, so the notification leaks nothing.
