# ADR 0001: FastMCP proxy wiring for upstreams

Status: accepted (WP1, 2026-10-15). Verified against `fastmcp==4.1.0` source:
`fastmcp/server/server.py` (`create_proxy`), `fastmcp/server/providers/proxy.py`
(`ProxyClient`, `_create_client_factory`, `ProxyProvider`),
`fastmcp/client/transports/base.py` (`TransportOptions`), `transports/http.py`, `transports/sse.py`.

## Context

Each upstream must appear as a namespaced set of tools, and the caller's `Authorization`
header must never reach an upstream (MCP spec 2026-07-28 authorization; PLAN decision 5).
FastMCP proxies forward inbound headers upstream by default.

## What the source says

- `create_proxy(target, *, mode=None, **settings)` has no `forward_incoming_headers`
  parameter. For a URL target it builds `ProxyClient(target)` internally, whose default is
  `forward_incoming_headers=True`. `**settings` are `FastMCP(...)` kwargs, so passing the
  flag there fails.
- Header forwarding is a client transport option: `TransportOptions.forward_incoming_headers`
  (plain clients default `False`; `PROXY_TRANSPORT_OPTIONS` sets `True`). HTTP and SSE
  transports honour it; stdio ignores it.
- When `create_proxy` gets a `Client` instance, the factory calls `client.new()` per request
  and `_with_proxy_transport_options()` only overrides `session_class`, so the client's own
  `forward_incoming_headers` value is preserved.
- A `Client` target carries its own protocol `mode`; the per-request era mirroring that a
  URL target gets (`_mirror_front_era_mode`) is skipped. `ProxyClient` defaults to
  `mode="legacy"`.

## Decision

```python
transport = StreamableHttpTransport(url, headers=static_headers)  # or SSETransport
client = ProxyClient(transport, forward_incoming_headers=False, mode=cfg.protocol_mode)
proxy = create_proxy(client, name=upstream_id, provider_error_strategy="warn")
mcp.mount(proxy, namespace=upstream_id)
```

- `forward_incoming_headers=False` on the `ProxyClient` is the supported switch and survives
  the per-request `client.new()` copy.
- `protocol_mode` is per upstream (default `auto`): `auto` probes `server/discover` per
  backend connection and falls back to the handshake; `2026-07-28` pins the modern era for
  known FastMCP 4.x upstreams (one POST per call); `legacy` pins the handshake. Measured
  locally for list+call on a fresh connection: auto ~450 ms, pinned modern ~20 ms, legacy ~25 ms.
- Upstream credentials (WP4) go into the transport's static `headers=`/`auth=`; they are the
  gateway's own.
- `ProxyProvider` caches component lists (`_cache_ttl`, default 300 s) and `create_proxy`
  exposes no knob, so the registry sets `provider._cache_ttl` from `cacheTtlSec` after
  construction (private attribute; re-check on upgrade).
- `provider_error_strategy="warn"`: a failing upstream is skipped in aggregate listings
  instead of failing the whole `tools/list`.

## Consequences

- Wire names are `<upstream id>_<tool>`: `public_time_now`, `public_echo`,
  `private_secret_lookup` on the local stack.
- A backend session per request means no state is shared between callers. A
  `sessionMode: shared` upstream (one reused connected client) is WP4's decision; FastMCP
  allows it only for stateless HTTP backends.
