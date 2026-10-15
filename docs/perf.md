# Performance and SLOs

How fast the gateway is on a developer Mac, what we promise in production, and how to
measure it again. The load test lives in [`loadtest/`](../loadtest); `make loadtest` runs it.
The failure drills (`make drills`) cover behaviour under faults; this page covers latency
and capacity.

## SLOs

These apply per gateway pod, measured inside the cluster (from the router to the pod and
back, without the client's WAN latency). Each one has a k6 threshold in
[`loadtest/main.js`](../loadtest/main.js) and, where it can be watched in production, an
alert or a dashboard query.

| SLO                         | Target                                   | Measured by                                                                                  |
| --------------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------- |
| `tools/list`, list cached   | p95 < 150 ms                             | k6 `mcp_list_cached`, `mcp_list_dev1`; prod: `gateway_request_duration_seconds{method="tools/list"}` |
| `tools/list`, list not cached | p95 < 600 ms                           | k6 `mcp_list_cold`, `mcp_list_cold_negotiate`                                                |
| `tools/call` gateway overhead | p95 < 300 ms with a fast upstream      | k6 `mcp_call_guest`, `mcp_call_dev1`; prod: `gateway_tool_call_duration_seconds` minus upstream time |
| Availability of `/mcp`      | ≥ 99.9 % of responses are not HTTP 5xx, measured monthly | `gateway_http_responses_total{route="mcp"}`; alert `McpGatewayHigh5xxRatio` (> 2 % over 5 min) |
| Rolling restart             | zero 5xx for clients during a rollout    | drill `test_rolling_restart` (two orderings)                                                 |
| Policy propagation          | an allow-list edit reaches every pod within 2 s (change stream); never later than 5 min (alert) | `make smoke`, drill `test_mongo_restart`; alert `McpGatewayPolicyStale` |

The SLOs leave the upstream's own latency out of `tools/call`, because the gateway does
not control it. Per-upstream call timeouts are in ADR 0009. A tool error or an upstream
failure is an MCP error inside an HTTP 200 and does not count against availability. The
5xx SLO tracks failures of the gateway itself.

## Results (2026-10-15)

Run with `make loadtest` (60 s, default rates) on this machine:

- MacBook with an Apple M5 (10 cores), 24 GiB RAM, macOS 26.5.2
- gateway on the host: Python 3.12.13, FastMCP 4.1.0, one uvicorn worker per process,
  INFO JSON audit logs written to a file, rate limits raised (all k6 virtual users share one IP)
- Mongo 8 (single-node replica set), Keycloak 26.8 and the two sample upstreams in Podman
  6.1.4 (applehv VM, 5 vCPU, 2 GiB), so each upstream round trip crosses the VM network
- k6 2.3.0, native on the host

Load (all scenarios run at the same time): guests at 40 iterations/s (70 % `tools/list`,
30 % `tools/call sample-public_echo`), dev1 at 10 iterations/s (list + call
`sample-private_secret_lookup`, bearer token), 50 virtual users each holding a
`subscriptions/listen` stream open, plus cold lists on two more gateways. That is about 60 MCP
requests/s on the warm gateway plus 50 open streams.

| Operation                                                   | Requests | p50 ms | p95 ms | p99 ms | max ms |
| ----------------------------------------------------------- | -------: | -----: | -----: | -----: | -----: |
| tools/list cached (guest)                                   |     1677 |    3.7 |    7.0 |   11.6 |   42.5 |
| tools/list cached (dev1)                                    |      601 |    6.0 |    8.8 |   25.7 |   50.6 |
| tools/list cold, shared upstream sessions (guest)           |      300 |    4.9 |    6.9 |    8.5 |   27.6 |
| tools/list cold, per-request sessions + negotiation (guest) |      121 |   10.4 |   12.3 |   28.0 |   31.3 |
| tools/call guest (`sample-public_echo`)                     |      724 |    6.9 |   13.2 |   28.7 |   40.3 |
| tools/call dev1 (`sample-private_secret_lookup`)            |      601 |    7.5 |   11.3 |   30.4 |   35.8 |

The MCP-level error rate was 0.00 %, there were 0 HTTP 5xx, all thresholds passed, and 50
listen streams were open at the peak.

What the rows mean:

- **cached**: the warm gateway runs the production configuration (Mongo policy, upstreams
  from Mongo, seeded `cacheTtlSec: 60`), so the upstream tool lists come from the proxy
  cache and the effective tool set from the per-principal memo. dev1 is about 2 ms slower
  than a guest because every request verifies a JWT (RS256 signature, cached JWKS).
- **cold, shared upstream sessions**: `cache_ttl_sec: 0`, so every `tools/list` fans out
  to both upstreams over their already open sessions. With local upstreams this costs
  about 1 ms more than the cache. In production the cold cost is the upstream's own
  `tools/list` latency.
- **cold, per-request sessions + negotiation**: the worst case. Each list opens a new
  connection per upstream and negotiates the protocol version (`protocolMode: auto`,
  `server/discover`) before it can list. Upstreams that are not pinned to a protocol
  version cost about 2× here (ADR 0009). Pin `protocolMode` and use `sessionMode: shared`
  where the upstream allows it.
- **dev1 tools/call** goes to `sample-private`, which is reached through the same proxy
  path as `sample-public`.

### Capacity of one pod

The same mix with `LOADTEST_SCALE=3|5|10` (30 s each) finds where one process saturates.
Only the warm gateway's numbers are shown; the cold gateways were not loaded hard.

| Scale | Offered req/s (warm) | list p95 | call p95 | Errors | Warm gateway CPU (avg / peak of one core) |
| ----: | -------------------: | -------: | -------: | -----: | ----------------------------------------: |
|     1 |                  ~60 |     7 ms |    13 ms |    0 % |                         (not sampled at 1×) |
|     3 |                 ~180 |    70 ms |   137 ms |    0 % |                                65 % / 92 % |
|     5 |                 ~300 |   2.2 s  |   4.3 s  |    0 % |                                70 % / 98 % |
|    10 |                 ~600 |   6.7 s  |   9.0 s  | 6.6 %  |                                83 % / 99 % |

One uvicorn worker is bound to one core. It holds the SLOs up to about **180 MCP
requests/s** of this mix. Beyond about **200 req/s** requests queue: latency rises steeply,
the gateway still answers (no 5xx), and timeouts set in at 10× the default rates. The
scaling guidance in [runbook.md](runbook.md#scaling)
follows from these numbers: target about 100 req/s per pod and scale out, never add
workers.

These numbers come from a laptop with fast local upstreams. Treat them as the gateway's
own overhead and ceiling, not as production latency. Re-run the load test on the target
cluster (in-cluster k6 against the Service) before setting HPA targets.

## Reproducing

```bash
make up && make seed              # local stack + seeded policy
brew install k6                   # or let run.sh use the grafana/k6 container
make loadtest                     # 60 s, SLO thresholds, writes loadtest/results/
LOADTEST_SCALE=3 LOADTEST_DURATION=30s make loadtest   # capacity probing (may fail thresholds)
```

`loadtest/run.sh` starts three host gateways on :18080 (warm), :18081 (cold, shared
sessions) and :18082 (cold, per-request sessions with negotiation), warms the first one,
runs [`loadtest/main.js`](../loadtest/main.js) and stops them. Scenario scripts:
[`guest.js`](../loadtest/guest.js), [`dev1.js`](../loadtest/dev1.js),
[`listen.js`](../loadtest/listen.js) and [`cold.js`](../loadtest/cold.js), with the raw MCP
helpers in [`lib/mcp.js`](../loadtest/lib/mcp.js) (stateless 2026-07-28 requests: `_meta`
envelope plus `Mcp-Method`/`Mcp-Name` headers). The output is
`loadtest/results/summary.{md,json}`, the gateway logs, `k6.log`, and samples of the
listen-stream gauge and CPU. Listen-stream requests end by client timeout. They are tagged
`op=listen` and kept out of the thresholds.
