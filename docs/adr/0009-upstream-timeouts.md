# ADR 0009: upstream timeouts, shared sessions and live mounting (WP4)

Status: accepted (WP4, 2026-10-15). Verified against `fastmcp==4.1.0` / `mcp==2.3.0` source:
`fastmcp/client/client.py` (`Client.__init__`, `new`, `_connect`, `_negotiate`),
`fastmcp/client/transports/http.py` and `sse.py` (`connect_session`),
`mcp/shared/_httpx_utils.py` (`McpHttpClientFactory`, `create_mcp_http_client`),
`fastmcp/server/providers/proxy.py` (`_create_client_factory`, `FastMCPProxy`, `ProxyTool.run`),
plus live experiments against the local stack.

## Question

How do `timeoutsMs.connect` / `timeoutsMs.call` from an `upstreams` document reach the
wire in FastMCP 4.1, how can `sessionMode: shared` be implemented safely, and how do
upstreams get mounted and unmounted while the gateway runs?

## Timeouts: what the source says

- `Client(timeout=X)` becomes `session_kwargs["read_timeout_seconds"]`: the MCP session's
  per-request deadline (how long one `tools/call` may take). It is **not** a connect
  timeout.
- `StreamableHttpTransport.connect_session` builds `httpx2.Timeout(30.0, read=X)` from that
  value and hands it to `httpx_client_factory(headers=, auth=, follow_redirects=,
  timeout=)` when a factory is given, else to `create_mcp_http_client` (30 s connect/pool,
  300 s read). The connect timeout is therefore **only** configurable through a custom
  `httpx_client_factory`. `sse_read_timeout` was removed from the HTTP transport in 4.0;
  `SSETransport` still takes it and passes the factory to `sse_client`.
- `Client(init_timeout=Y)` bounds the connect-time negotiation (`server/discover` probe or
  `initialize` handshake) via `anyio.fail_after`.
- `stdio` has no transport timeouts beyond `init_timeout` and the session read deadline.

## Decision (implemented in `gateway/src/mcp_gateway/upstreams/proxy.py`)

| Config               | Where it lands                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `timeoutsMs.connect` | `httpx2.Timeout(connect=, pool=)` in our `httpx_client_factory`, and `Client(init_timeout=)` |
| `timeoutsMs.call`    | `Client(timeout=)` (session read deadline) and `httpx2.Timeout(read=, write=)` for HTTP      |
| SSE                  | same, but `read=None` on the httpx client: the event stream must stay open between calls    |
| `cacheTtlSec`        | `ProxyProvider._cache_ttl` (ADR 0001; private, re-check on upgrade)                         |
| `auth.*`             | static `headers=` on the transport (`Authorization: Bearer <secret>` or `<headerName>`)     |

Defaults when a document omits `timeoutsMs`: connect 2 s, call 60 s.

## Shared sessions

- `create_proxy(client)` with a **connected `ProxyClient`** does not reuse it: it logs
  "creating fresh sessions for each request" and clones per request. Reuse only happens for
  a connected plain `Client`, which would lose the proxy's forwarding session class. So for
  `sessionMode: shared` the registry builds `FastMCPProxy(client_factory=shared.get, ...)`
  directly, where `SharedClient.get()` returns one connected `ProxyClient`, reconnecting
  when `is_connected()` turned false (upstream restarted). Proxy components wrap each call
  in `async with client:`, which on a connected client only bumps the nesting counter.
- Allowed only for `transport: http` with `protocolMode: 2026-07-28`: on the modern era
  every request is a self-contained POST, so one shared client is just a shared connection
  pool. A handshake-era session is stateful (and `SSETransport.legacy_only = True`), so
  `shared` is forced to `per_request` there and the reason is logged and reported in
  `/internal/upstreams/health`. The Mongo schema has an optional `protocolMode` (default
  `auto`), so a Mongo-sourced upstream must set `protocolMode: 2026-07-28` explicitly to
  share a session; the local seed does so for both sample upstreams.
- Measured on the local stack (`sample-private`, pinned modern): per_request ~20 ms per
  call, shared ~5 ms per call after the first; shared survives `podman stop/start` of the
  upstream (one failed call, then `SharedClient` reconnects).
- Caveat: `ProxyTool.run` stashes the proxy request context on the shared `ProxyClient`
  before each backend call, so concurrent calls overwrite it. That only affects
  server-initiated roots/sampling/elicitation forwarding, which the stateless gateway does
  not use (v1 exposes tools only).

## Live mount / unmount

- Mount: `mcp.mount(proxy, namespace=id)` appends exactly one wrapped provider to the
  public list `mcp.providers`; the registry remembers that object. The registry raises if
  `mount` ever appends anything but one entry, and `tests/unit/test_upstream_registry.py`
  checks on a real `FastMCP` that removing the entry makes the tools disappear (ADR 0005).
- Unmount: `mcp.providers.remove(provider)` (identity equality on providers), close the
  shared client if any, drop the `gateway_upstream_up{upstream}` series.
- Reconcile: `apply()` compares the mount fingerprint (`UpstreamConfig` minus
  `updatedAt`/`updatedBy`); a changed document is unmounted and mounted again.
- Health: a fresh `base_client.new()` per probe (independent of the shared session), one
  `tools/list` under `anyio.fail_after(GATEWAY_UPSTREAM_PROBE_TIMEOUT_SEC)`. The proxy stays
  mounted while down: `tools/list` still answers for the other upstreams
  (`provider_error_strategy="warn"`), calls to the dead upstream fail after its connect
  timeout with `Client failed to connect: ...`.

## Consequences

- Operators tune one document, not code: `timeoutsMs`, `cacheTtlSec`, `sessionMode`.
- Changing PLAN decision 10: removals and disables now take effect live, not on the next
  rolling restart (policy still hides them first).
- Private attributes relied on: `ProxyProvider._cache_ttl`, `Client._transport_options`
  (read in tests only). Both are covered by unit tests that fail on upgrade.
