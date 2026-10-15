# ADR 0005: unmounting a mounted provider at runtime (verify d)

Status: verified (WP1, 2026-10-15). Option for WP4.

## Question

Can an upstream mounted with `mcp.mount(proxy, namespace=...)` be removed while the server is
running, or do removals only take effect on the next rolling restart (PLAN decision 10)?

## Evidence

Source: `FastMCP` inherits `AggregateProvider` (`fastmcp/server/providers/aggregate.py`),
which keeps `self.providers: list[Provider]` and iterates it on every list/get call.
`mount()` appends `_WrappedProvider(FastMCPProvider(server), transforms=[Namespace(ns)])`.
There is no `unmount`, `remove_provider` or similar on `FastMCP`
(`hasattr(mcp, "remove_provider") is False`); only `local_provider.remove_tool()` exists for
locally registered components.

Experiment (in-memory client):

```
mount child as "c"        -> tools: ["c_hello"]
mcp.providers.remove(p)   -> tools: []        call c_hello -> ToolError: Unknown tool: 'c_hello'
```

## Findings

- Runtime removal works by removing the entry from the public `providers` list; lookups
  take effect immediately and nothing is cached at the aggregate level.
- Caveats: the mounted proxy's lifespan (trivial for proxies) is not stopped, and the
  extension runtime scopes registered at mount time are left in place until shutdown. Both
  are harmless for `FastMCPProxy` instances.

## Decision

WP1 keeps PLAN decision 10: disabled upstreams are hidden by policy immediately and
physically unmounted on restart. `UpstreamRegistry` retains the mounted provider objects so
WP4 can implement live removal with `mcp.providers.remove(provider)` if wanted; that is a
list mutation on a public attribute, not an API, so guard it with a test on upgrade.
