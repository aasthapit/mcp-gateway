# ADR 0006: what `server/discover` returns (verify e)

Status: verified (WP1, 2026-10-15).

## Question

Does the modern-era `server/discover` enumerate tools (which would bypass the per-request
tool filter), and does its content need filtering?

## Evidence

Source: `mcp/server/lowlevel/server.py` `_handle_discover` returns
`DiscoverResult(supported_versions, capabilities, instructions)`; `mcp_types.DiscoverResult`
has exactly those fields plus `result_type` and the cache hints. FastMCP routes the request
through the `on_discover` middleware hook (`fastmcp/server/low_level.py`).

Experiment (raw modern request to the gateway):

```json
{
  "supportedVersions": ["2026-07-28"],
  "capabilities": {
    "logging": {},
    "prompts": {"listChanged": false},
    "resources": {"subscribe": false, "listChanged": false},
    "tools": {"listChanged": false},
    "extensions": {"io.modelcontextprotocol/ui": {}}
  },
  "instructions": "MCP gateway. Tools are namespaced as <upstream>_<tool>. ...",
  "ttlMs": 30000, "cacheScope": "private", "resultType": "complete",
  "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "gateway", "version": "0.1.0"}}
}
```

## Findings

- No tool, resource or prompt names appear. Nothing principal-specific leaks.
- `instructions` is the gateway's own text; mounted proxies' `ProxyMetadataMiddleware`
  only affects the proxy servers themselves, not the parent's discover result.
- `capabilities` advertise prompts and resources even though policy hides them (lists come
  back empty, reads answer not-found). Spec-wise that is allowed; clients just see empty
  lists.

## Decision

No `on_discover` filtering in WP1. If a later WP wants to hide the prompts/resources
capabilities, `on_discover` can return a modified `DiscoverResult`.
