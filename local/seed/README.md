# local/seed

Example policy documents loaded by `make seed` (upsert with `w: majority`, then
`meta.version += 1`). They validate against `../../schemas/*.schema.json`
(`make validate-seed`). `updatedAt`/`updatedBy` are set by `seed.js`.

Effective tools with this seed:

| Caller                         | Grants                   | Tools                                                                                                                           |
| ------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| guest                          | anon-default             | `sample-public_time_now`, `sample-public_echo`                                                                                  |
| dev1 (role `mcp-developer`)    | anon-default, developers | guest + `sample-private_secret_lookup`                                                                                          |
| ci-runner (client credentials) | anon-default, ci-bot     | `sample-public_time_now` only: `ci-tools` denies `sample-public_echo`, and a deny wins over every allow in the union (PLAN 3.2) |

Upstream URLs point at `localhost:8081/8082`: the gateway runs on the host (`uv run
mcp-gateway`, tests, `make smoke`) and reaches the sample containers through their
published ports. A containerised gateway needs `http://host.containers.internal:8081/mcp`
(Podman) or `http://sample-public:8081/mcp` (same compose network) instead; edit the
`upstreams` documents, the gateway re-reads them on `POST /internal/upstreams/reload`
(and WP3's watcher does it automatically).
