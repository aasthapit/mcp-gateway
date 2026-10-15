# ADR 0008: policy hot reload (change stream + version poll)

Status: accepted (WP3, 2026-10-15). Implements docs/PLAN.md 3.2 / 3.3 / 3.1 step 3.

## Context

Every gateway replica must apply an admin's allow-list edit within seconds, without a
restart, without sticky sessions or shared in-process state (PLAN 1, decision 7). The
policy lives in MongoDB (`upstreams`, `toolsets`, `grants`, `meta`), admin writes use
`w: majority` and bump `meta.version` in the same transaction.

## Decision

One `PolicyWatcher` task per process, fed by `MongoPolicyStore.watch()`, which merges:

1. **One database change stream** (`db.watch`, pipeline `$match ns.coll in [4 collections]`,
   `fullDocument: updateLookup`, `maxAwaitTimeMS 1000`). Resume token kept in memory;
   resumable errors reopen with `resumeAfter`; `ChangeStreamHistoryLost` (286),
   `ChangeStreamFatalError` (280), `CappedPositionLost` (136) and `invalidate` drop the
   token. Every (re)open **without** a token also emits a reload event: this closes the
   gap between the initial `load()` and the stream start (and after a lost history) at the
   cost of one cheap extra reload. Backoff 0.5 s doubling to 10 s.
2. **A `meta.version` poll** every `GATEWAY_POLICY_POLL_SEC` (default 30) with
   `readConcern: majority`, firing when the stored version differs from the last loaded
   one. It keeps correctness when the stream is down or unavailable (Atlas tiers), at
   poll latency.

The watcher debounces events for 250 ms (`GATEWAY_POLICY_DEBOUNCE_MS`), then runs a
**full reload** (`load()` re-reads the version before and after the three collection
scans and retries up to three times if it moved), builds an immutable `PolicySnapshot`
(validated with the pydantic mirrors of `schemas/`, invalid documents skipped and counted
in `gateway_policy_invalid_documents`), swaps it atomically into a `PolicyHolder`, calls
`registry.apply(snapshot)` (section 5 interface; WP4 mounts new upstreams there) and
publishes `ToolsListChanged()` on the in-process `InMemorySubscriptionBus` whose
`ListenHandler` is registered on the low-level server in `app.py` (ADR 0003). A failed
reload keeps the previous snapshot. No partial updates from stream payloads: the
`fullDocument` is only used to carry the version hint; correctness never depends on it.

Readiness: `policy` (critical) is green once any snapshot is loaded and stays green on a
stale one, reporting `policy_age_seconds`, `version`, `last_error`, `change_stream_up`,
`resumes`, `full_reopens`; `policy_stream` (non-critical) shows the stream state.
Liveness is untouched. Metrics: `gateway_policy_version`, `gateway_policy_age_seconds`
(live gauge via `set_function`), `gateway_policy_reloads_total{initial|stream|poll}`,
`gateway_policy_reload_failures_total`, `gateway_change_stream_up`.

Driver: **pymongo 4.19 async API** (`AsyncMongoClient`), not motor (deprecated, and
fastmcp does not pull either in). The client is created lazily on the loop that first
uses it because tests and the smoke run uvicorn on a thread.

Evaluation semantics are the plan's: union of `allow` over all matching enabled grants'
enabled toolsets minus the union of their `deny`, fnmatch globs (anchored, case-sensitive,
one compiled regex per list), deny by default, memoised per `Principal.cache_key`
(bounded at 10 000 entries per snapshot). `anonymous` grants match every principal, so
the guest set is the floor; a deny in any matching toolset wins over the floor's allow
(`local/seed/README.md` said otherwise for `ci-runner` and was corrected).

## Verified on the local stack (tests/integration/test_policy_live.py, make smoke)

| Scenario                                                                         | Result                                                                                                                                                                                           |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Edit `toolsets/public-basics` + version bump in a majority transaction           | Next `tools/list` reflects it after ~0.36 s (debounce + load); `subscriptions/listen` stream receives `notifications/tools/list_changed`; the revert propagates the same way.                   |
| Guest calls a hidden tool                                                        | `Unknown tool: 'sample-private_secret_lookup'`, byte-identical to FastMCP's own unknown-tool error; `gateway_policy_denials_total` incremented.                                                   |
| `db.adminCommand({replSetStepDown: 1, force: true})` (single node, ~1.1 s)        | pymongo resumes the cursor transparently inside `try_next` (`InterruptedDueToReplStateChange` is resumable); the gateway logs nothing, stays ready, and a later edit propagates normally.         |
| `killOp` on the stream's `getMore`                                               | Same: transparent in-cursor resume. The poll also fired for the version it found newer than the loaded one.                                                                                      |
| `podman restart mcp-mongo` (one-off drill)                                       | Gateway keeps serving the stale snapshot, stays ready, stream and poll back off to 10 s with `ServerSelectionTimeoutError` warnings. The container itself did not come back (see below).          |

## Surprises

- **FastMCP 4.1 still does not serve `subscriptions/listen`**; registering the SDK
  `ListenHandler` via `mcp._mcp_server.add_request_handler` (ADR 0003) works in stateless
  mode, and `server/discover` then advertises `tools.listChanged: true`. The fastmcp
  `Client` has no `listen()`; tests use `mcp.client.subscriptions.listen(client.session,
  tools_list_changed=True)`.
- **pymongo resumes on its own** for the common failover errors, so the gateway's own
  resume path (reopen with the saved token) is only reached for errors the driver deems
  non-resumable (e.g. server selection timeout while Mongo is away). The `resumes` /
  `full_reopens` counters tell which path ran.
- **The seed's upstream ids (`sample-public`) and WP1's static ids (`public`) differed**,
  so the seeded toolsets could never match the mounted tools. WP3 aligned the static
  defaults with the seed; the upstream id is the tool namespace and must agree between
  `upstreams._id` and the names toolsets use.
- **A denied call must not be a distinct error.** The task sketched
  `ToolError("Tool not found")`, but FastMCP answers unknown tools with
  `NotFoundError("Unknown tool: '<name>'")`; using anything else would make hidden tools
  distinguishable from non-existent ones. `PolicyDenied` therefore subclasses
  `NotFoundError` with the same message (WP1 design, kept; test
  `test_denied_matches_unknown_tool_error`).
- **`local/compose.yaml` Mongo cannot survive `podman restart`**: its entrypoint
  regenerates `/tmp/mongo-keyfile`, which already exists as a 400 file from the first
  start, so the container crash-loops with `Permission denied`. Recreating the container
  (`compose up -d --force-recreate --no-deps mongo` + `mongo-init`) recovers it with the
  data volume intact. Follow-up for WP0: write the keyfile only if missing (or `rm -f`
  first). Because of this the chaos test uses the step-down, not a container restart.
- Prometheus counters are process-global, so integration tests that start several
  in-process gateways must assert on deltas, not absolute counts.
- Admin writes that forget the `meta.version` bump are still picked up by the stream but
  not by the poll; the admin API (WP5) must keep bumping it in the same transaction.

## Consequences

- Propagation latency is ~0.3 to 0.5 s with the stream, at most `GATEWAY_POLICY_POLL_SEC`
  without it. Alert on `gateway_policy_age_seconds` (> 5 min) and
  `gateway_change_stream_up == 0` (> 2 min), as PLAN 3.6 lists.
- Each pod opens exactly one change stream (one uvicorn worker per pod, PLAN 3.6); Mongo
  pool size must exceed one per pod.
- Legacy-era clients get no push and re-list on their own; modern clients without a
  listen stream re-list at `ttlMs` (30 s) expiry.
