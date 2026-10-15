# ADR 0002: `ttlMs` / `cacheScope` on `tools/list` (verify a)

Status: verified (WP1, 2026-10-15).

## Question

Can the server attach `ttlMs` and `cacheScope` to `tools/list` results on the 2026-07-28
era, so clients re-list soon after a policy change and never reuse a list across principals?

## Evidence

Source: `fastmcp/server/caching.py` (`build_cache_hints`), `fastmcp/server/server.py`
(`FastMCP(cache_ttl=, cache_scope=)`), `mcp/server/runner.py` (`_serialize` fills
`ttlMs`/`cacheScope` from `server.cache_hints`; handler-set fields win).

Experiment (gateway in-process, raw JSON-RPC with the modern `_meta` envelope and
`Mcp-Method` header):

```
tools/list      -> {"ttlMs": 30000, "cacheScope": "private", "resultType": "complete", ...}
server/discover -> {"ttlMs": 30000, "cacheScope": "private", ...}
legacy client (2025-11-25 handshake) -> ListToolsResult.ttl_ms == 0, nothing on the wire
```

## Findings

- Yes. `FastMCP("gateway", cache_ttl=<seconds>, cache_scope="private")` applies one
  server-wide hint to every cacheable method (`tools/list`, `prompts/list`,
  `resources/list`, `resources/templates/list`, `resources/read`, `server/discover`).
- Modern era only; legacy clients get no hint and re-list on their own schedule.
- No per-request hook: middleware `on_list_tools` returns `Sequence[Tool]`, and the SDK
  runner stamps the hint afterwards. Not needed.

## Decision

`GATEWAY_TOOLS_LIST_TTL_SEC` (default 30) feeds `cache_ttl`; `cache_scope` is fixed to
`private` because the list depends on the bearer presented. WP3 can treat TTL expiry as the
universal fallback for propagating policy changes (push is ADR 0003).
