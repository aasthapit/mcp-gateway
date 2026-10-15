# Architecture decision records

Short records of decisions and of the FastMCP 4.1 behaviours the plan flagged as
**[verify]** (docs/PLAN.md section 9). Each verification ADR states what was checked, how
(source in the pinned `fastmcp==4.1.0` / `mcp==2.3.0` venv plus a live experiment against
the local stack), the result, and what the gateway does about it.

| ADR                                                  | Topic                                                    |
| ---------------------------------------------------- | -------------------------------------------------------- |
| [0001](0001-fastmcp-proxy-wiring.md)                 | How upstreams are proxied; disabling header forwarding   |
| [0002](0002-tools-list-ttl-cache-scope.md)           | `ttlMs` / `cacheScope` on `tools/list` (verify a)        |
| [0003](0003-list-changed-notifications-stateless.md) | `notifications/tools/list_changed` in stateless mode (b) |
| [0004](0004-sse-keepalive.md)                        | SSE keep-alive comments on long streams (c)              |
| [0005](0005-unmount-provider-at-runtime.md)          | Unmounting a provider at runtime (d)                     |
| [0006](0006-server-discover.md)                      | What `server/discover` returns (e)                       |
| [0007](0007-jwt-verification.md)                     | JWT verification on `JWTVerifier`; RFC 9728 metadata     |
| [0008](0008-policy-reload.md)                        | Policy hot reload: change stream + version poll (WP3)    |
| [0010](0010-openshift-packaging.md)                  | OpenShift packaging: Helm via Argo CD, Routes, Percona  |
