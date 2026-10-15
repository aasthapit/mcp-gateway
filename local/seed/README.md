# local/seed

Example policy documents loaded by `make seed` (upsert with `w: majority`, then
`meta.version += 1`). They validate against `../../schemas/*.schema.json`
(`make validate-seed`). `updatedAt`/`updatedBy` are set by `seed.js`.

Effective tools with this seed:

| Caller                         | Grants                   | Tools                                                                                      |
| ------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------ |
| guest                          | anon-default             | `sample-public_time_now`, `sample-public_echo`                                             |
| dev1 (role `mcp-developer`)    | anon-default, developers | guest + `sample-private_secret_lookup`                                                     |
| ci-runner (client credentials) | anon-default, ci-bot     | guest set (deny of `echo` only applies inside `ci-tools`; the guest floor still allows it) |
