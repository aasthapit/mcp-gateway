# MCP gateway runbook

For whoever is on call for `mcp-gateway`. Each alert below lists symptoms, checks, then
actions. The alerts are defined in
[`deploy/helm/mcp-gateway/templates/prometheusrule.yaml`](../deploy/helm/mcp-gateway/templates/prometheusrule.yaml)
and unit-tested in [`deploy/alerts/alerts_test.yaml`](../deploy/alerts/alerts_test.yaml).
The procedures at the end cover policy rollback, secret rotation and scaling.

Conventions: namespace `mcp-gateway`, Deployment and Service `mcp-gateway`, metrics on
port 9090 (`/metrics` also on 8080). `oc` works the same as `kubectl` here. `$POD` is one
gateway pod. `$IT` is the internal token, taken from the `mcp-gateway-env` Secret, key
`GATEWAY_INTERNAL_TOKEN`.

```bash
oc -n mcp-gateway get pods -l app.kubernetes.io/name=mcp-gateway -o wide
oc -n mcp-gateway exec $POD -- curl -s localhost:8080/readyz | jq .          # every readiness check, with details
oc -n mcp-gateway exec $POD -- curl -s localhost:9090/metrics | grep ^gateway_
oc -n mcp-gateway logs $POD --since=15m | jq -c 'select(.level=="error" or .level=="warning")'
IT=$(oc -n mcp-gateway get secret mcp-gateway-env -o jsonpath='{.data.GATEWAY_INTERNAL_TOKEN}' | base64 -d)
oc -n mcp-gateway exec $POD -- curl -s -H "X-Internal-Token: $IT" localhost:8080/internal/upstreams/health | jq .
```

Logs are JSON lines. Audit events have `"audit": true` and an `event` of `tools.call`,
`tools.call.denied` or `auth.rejected`. With `TELEMETRY_MODE=native` every line written
inside a request carries `trace_id`, which is the trace to open in the tracing backend.

How the gateway behaves under failure (each case is verified by `make drills`, see
[perf.md](perf.md)):

| Failure                       | Effect on clients                                                                 |
| ----------------------------- | --------------------------------------------------------------------------------- |
| Mongo down or failing over    | None. Each pod keeps serving its last snapshot; edits wait until Mongo is back.   |
| One upstream down             | Only that upstream's tools fail, as fast MCP errors (no 5xx); the rest work.      |
| IdP down                      | Tokens with a cached `kid` keep working; a new `kid` gets 401 within ≤ 3 s.        |
| Pod restart or rollout        | No 5xx. Listen streams get a graceful close and clients re-listen.               |

---

## McpGatewayPolicyStale

**Fires when:** `gateway_policy_staleness_seconds > 300` on a pod for 1 minute. This
gauge is the time since Mongo last confirmed that the pod's snapshot is current (a load, or
a version poll that matched). It is not `gateway_policy_age_seconds`, which counts the time
since the last policy change and grows normally while nobody edits the policy.

**Symptoms:** Allow-list edits do not show up on that pod. This includes revocations: a
removed grant still applies there. Users get different tool lists depending on which pod
answers them.

**Checks**

1. Run `readyz | jq .checks.policy` on the pod and look at `last_error`,
   `change_stream_up`, `loaded_version` and `policy_staleness_seconds`.
2. Look for `policy version poll failed` and `policy reload failed` in the pod's logs.
3. Check that Mongo is reachable from the pod. Look at the Percona CR status
   (`oc get psmdb mcp-mongo`), at the NetworkPolicy egress rule to Mongo, at whether the
   `gateway` user's password was rotated without updating `GATEWAY_MONGO_URI`, and at the
   TLS CA mount (`/etc/gateway/mongo-ca/ca.crt`).
4. If every pod is stale, Mongo or the credentials are at fault. If only one pod is stale,
   that pod's network or node is at fault.

**Actions**

- Mongo is down: restore Mongo first (Percona runbook). The gateway recovers by itself:
  the next poll (within 30 s) or a reopened change stream reloads the snapshot. No
  restart is needed.
- Credentials are wrong: fix the Secret (see [rotation](#rotate-mongo-credentials)), then
  run `oc rollout restart deploy/mcp-gateway`.
- One pod is stale: delete that pod (`oc delete pod $POD`); the PDB keeps 2 others serving.
- If a revocation must take effect right now and Mongo cannot be fixed quickly, scale the
  stale pods away or set `ALLOW_ANONYMOUS=false` as a temporary measure. Both are coarse;
  prefer fixing Mongo.

## McpGatewayChangeStreamDown

**Fires when:** `gateway_change_stream_up == 0` on a pod for 2 minutes.

**Symptoms:** Policy edits take up to `POLICY_POLL_SEC` (30 s) to arrive instead of under
2 s. Nothing else changes.

**Checks**

1. Run `readyz | jq .checks.policy_stream` (it is non-critical and never flips
   readiness). Then run `jq .checks.policy` and look at `change_stream_error`, `resumes`
   and `full_reopens`.
2. Look for `policy change stream error` and `history lost` in the logs. Constant "history
   lost" means the oplog window is shorter than the outage. Raise
   `oplogMinRetentionHours` on the replica set.
3. Change streams need a replica set, and the `gateway` user needs the `changeStream`
   action on `mcp_gateway` (the built-in `read` role includes it).

**Actions:** The watcher retries on its own with backoff up to 10 s and resumes from its
token. If the stream stays down while Mongo is otherwise healthy, restart the pod. If it
is down on every pod, check the Mongo user's roles and the replica set status. The policy
stays correct in the meantime (the poll still runs), so this is not urgent unless
McpGatewayPolicyStale fires as well.

## McpGatewayUpstreamDown

**Fires when:** every gateway pod's probe of an upstream has failed for 5 minutes
(`max by (upstream) (gateway_upstream_up) == 0`). If only one pod sees the upstream down,
that is a network problem on that pod and does not fire this alert.

**Symptoms:** `tools/call` to `<upstream>_*` returns an MCP error (`Client failed to
connect`, or a timeout after the upstream's `timeoutsMs.connect`, 2 s by default). Its
tools may still be listed from the cache for up to `cacheTtlSec`. All other upstreams work.

**Checks**

1. Run `/internal/upstreams/health` (command above) and look at `status`, `reason` and
   `error` for the upstream. `secret_missing`, `invalid_config` and `mount_failed` are
   gateway-side configuration problems. `down` means the upstream itself is unreachable.
2. Look at `gateway_upstream_errors_total{upstream=...}` by `kind`.
3. Check the upstream's own Deployment in `mcp-upstreams`, and the NetworkPolicy egress
   rule from the gateway to that namespace.

**Actions**

- Upstream outage: page the upstream's owners. The gateway reconnects by itself once the
  upstream is back (the probe runs every 30 s, and calls retry on every request).
- `secret_missing`: the Secret file named by `auth.secretRef` is missing. Fix the
  ExternalSecret, then restart (see [upstream secrets](#rotate-upstream-credentials)).
- To stop clients from trying a dead upstream: disable it (`enabled: false` in the
  `upstreams` document, through the admin UI or API). Every pod unmounts it within about
  2 s (ADR 0009).

## McpGatewayHigh5xxRatio

**Fires when:** more than 2 % of `/mcp` HTTP responses over 5 minutes are 5xx. Tool and
upstream errors are MCP errors inside an HTTP 200, so a 5xx means the gateway itself
failed.

**Symptoms:** Clients see HTTP 500, 502 or 503 from the Route.

**Checks**

1. Run `sum by (pod) (rate(gateway_http_responses_total{route="mcp",code="5xx"}[5m]))` to
   see whether one pod or all of them are failing.
2. Search the logs for `Traceback` and for errors from `mcp.server` or `uvicorn`.
3. Check for a rollout in progress or just finished (`oc rollout history
   deploy/mcp-gateway`). A bad image shows up as all new pods failing.
4. Check for OOM kills (`oc get pods`, then `Last State: OOMKilled`) and for CPU saturation
   (see [Scaling](#scaling)). One pod saturates at about 200 req/s.
5. A 503 from the router (not from the gateway) means there were no ready endpoints. Look
   at `/readyz` on the pods.

**Actions:** Roll back a bad deploy (`oc rollout undo deploy/mcp-gateway`, or revert the
image digest in Git so Argo CD syncs it). Scale out when the pods are saturated. Restart a
single bad pod.

## McpGatewayDenialSpike

**Fires when:** policy denials exceed 1/s and are more than 5× the previous hour's rate,
for 5 minutes.

**Symptoms:** Clients call tools they cannot see. Either a client is probing, or a policy
change removed tools that clients still use (MCP clients cache tool lists for `ttlMs`, 30 s).

**Checks**

1. Find who and what: `logs | jq -c 'select(.event=="tools.call.denied") | {principal_kind, sub, client_id, tool}' | sort | uniq -c | sort -rn | head`.
2. Check for a recent policy change: `db.policy_history.find().sort({at:-1}).limit(10)`
   (see [rollback](#roll-back-a-policy-change)), or the History page in the admin UI.
3. Run `sum by (tool, principal_kind) (rate(gateway_policy_denials_total[5m]))`.

**Actions**

- A policy change broke legitimate use: [roll it back](#roll-back-a-policy-change).
- A single principal is probing: work out who owns the `client_id` or `sub`. Revoke the
  client at the IdP or add a deny grant. The rate limiter caps the request rate per pod
  in the meantime.

## McpGatewayRateLimitSpike

**Fires when:** more than 5 requests/s per principal kind are rejected by the per-pod
rate limiter, for 10 minutes.

**Symptoms:** Clients get the JSON-RPC error `-32000 Rate limit exceeded; retry later`.

**Checks**

1. Run `rate(gateway_rate_limited_total[5m])` by `principal_kind`. A spike under
   `anonymous` is keyed by client IP, a spike under `user` by `sub` (or `client_id`).
2. Anonymous traffic: check whether many guests share one IP behind a NAT or corporate
   proxy, or whether the router's X-Forwarded-For is not trusted. In the second case every
   guest is keyed by the router pod's IP. Check that `TRUSTED_PROXIES` covers the router
   pods' network (default `10.128.0.0/14`; with a HostNetwork ingress controller, add the
   node CIDR).
3. Find the noisy client in the audit log (`client_id`, `sub`) or the router's access log.

**Actions**

- Misconfigured `TRUSTED_PROXIES`: fix it in the values, then sync and restart.
- Legitimate traffic: raise `RATE_LIMIT_*_RPS` and `_BURST`. Remember that the limits
  are **per pod**: the real cap is the limit × the number of replicas, so the cap also
  moves when the HPA scales. Global limits (Redis) are v2 (ADR 0012).
- Abuse: block it at the IdP (users) or at the edge (IP).

---

## Roll back a policy change

Every admin write is one transaction: the document changes, `meta.version` is bumped, and
an entry is appended to `policy_history`:

```js
{ collection: "toolsets", id: "public-basics", op: "update", actor: "<sub>", at: ISODate(),
  version: 43, before: { ... }, after: { ... } }
```

1. Find the change:

   ```js
   use mcp_gateway
   db.policy_history.find({}, {collection:1, id:1, op:1, actor:1, at:1, version:1}).sort({at:-1}).limit(20)
   db.policy_history.findOne({collection: "toolsets", id: "public-basics", version: 43})
   ```

2. **Preferred: write `before` back through the admin API.** This makes the rollback an
   ordinary admin write, so it is validated, bumps the version and is recorded in history.
   Use the History page in the UI, or:

   ```bash
   curl -X PUT -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
     https://mcp-admin.<domain>/api/toolsets/public-basics -d @before.json
   # op was "create": DELETE /api/<collection>/<id>; op was "delete": PUT the `before` document
   ```

3. **If the admin API is down:** use mongosh with the `admin-api` user, in one transaction,
   keeping the same invariants. Paste this as one block:

   ```js
   const h = db.policy_history.findOne({ collection: "toolsets", id: "public-basics", version: 43 });
   const s = db.getMongo().startSession(); const d = s.getDatabase("mcp_gateway");
   s.startTransaction({ writeConcern: { w: "majority" } });
   if (h.before) d[h.collection].replaceOne({ _id: h.id }, h.before, { upsert: true });
   else d[h.collection].deleteOne({ _id: h.id });
   const v = d.meta.findOneAndUpdate({ _id: "policy" }, { $inc: { version: 1 }, $set: { updatedAt: new Date(), updatedBy: "runbook-rollback" } }, { returnDocument: "after" }).version;
   d.policy_history.insertOne({ collection: h.collection, id: h.id, op: "rollback", actor: "<you>", at: new Date(), version: v, before: h.after, after: h.before });
   s.commitTransaction();
   ```

4. Verify: every pod reports the new version within about 2 s:
   `oc exec $POD -- curl -s localhost:9090/metrics | grep '^gateway_policy_version'` on each
   pod, or `max(gateway_policy_version) - min(gateway_policy_version)` should be 0. Check the
   effective tools for an affected principal with the admin preview
   (`/internal/effective-tools`).

If a whole release of policy changes is bad, repeat the steps newest first. Do not restore
a Mongo backup to roll back policy: it would also discard every unrelated change made since.

## Rotate the internal token

`GATEWAY_INTERNAL_TOKEN` protects `/internal/*`, which only the admin API calls. The
gateway reads it from the `mcp-gateway-env` Secret, and the admin chart reads the same
Secret. Only one token is accepted at a time.

1. Write the new value in the secret store (Vault `mcp-gateway/internal`, property `token`).
2. Force the ExternalSecret to sync instead of waiting up to an hour:
   `oc annotate externalsecret mcp-gateway-env force-sync=$(date +%s) --overwrite`.
   Check that `oc get secret mcp-gateway-env` shows the new value.
3. Run `oc rollout restart deploy/mcp-gateway`. The rollout drops no MCP traffic (zero 5xx).
4. Run `oc rollout restart deploy/mcp-admin-api` straight after.

Between steps 3 and 4, admin catalog and preview calls can get 403 for a minute. MCP
clients are not affected. Accepting two tokens during rotation is a v2 item.

## Rotate upstream credentials

Upstream credentials are files in `/etc/gateway/upstream-secrets/<secretRef>`, from the
`mcp-gateway-upstreams` Secret (ESO `mcp-gateway/upstreams`). They are read when an
upstream is mounted, so changing the file alone does not affect a running pod.

**Without a restart (recommended):**

1. Add the new credential under a **new key**, for example `crm.token.v2`, keeping the old
   key, and force-sync the ExternalSecret. Wait about a minute for the kubelet to update the
   mounted files.
2. Change the upstream document's `auth.secretRef` to `crm.token.v2` (admin UI or API).
   The config fingerprint changes, so every pod re-mounts that upstream live with the new
   credential (ADR 0009).
3. Check `/internal/upstreams/health`: the upstream should be `up` on every pod. Then
   revoke the old credential at the upstream and remove the old key.

**With a restart:** update the value under the same key, force-sync, wait for the files to
update, then run `oc rollout restart deploy/mcp-gateway`. During the rollout, pods with the
old and the new credential run side by side, so the upstream must accept both until it ends.

## Rotate Mongo credentials

The Percona operator manages the `gateway` user. Change the password in the secret store
(both the user Secret the operator reads and `mcp-gateway/mongo` `uri`), force-sync both
ExternalSecrets, and then run `oc rollout restart deploy/mcp-gateway`.

Pods that still hold the old password keep serving their snapshot (the policy goes
stale but is not lost) until they restart. McpGatewayPolicyStale fires if one of them is
missed.

## Scaling

- **One worker per pod, always.** One uvicorn worker uses one core, and the design relies on
  exactly one policy watcher and one set of upstream clients per process. Scale with
  replicas.
- **Capacity:** one pod holds the latency SLOs up to about 180 MCP req/s of a list/call mix
  and saturates around 200 req/s on an Apple M5 core ([perf.md](perf.md#capacity-of-one-pod)).
  Plan for **100 req/s per pod** and re-measure on the cluster's CPUs.
- **HPA:** the default is CPU at 70 % (60 % in prod) of the **request**. With
  `requests.cpu: 500m` the pod can burst to its `2` core limit, but one worker never uses
  more than about 1 core. A pod is therefore saturated at about 200 % of the request, and
  the HPA scales well before that. Do not raise the target above about 150 % or the HPA
  reacts only once the pod is saturated.
- **Listen streams:** each open `subscriptions/listen` stream costs a connection and a
  small buffer. The gateway handled 50 streams per pod alongside full load without change.
  Turn on the `gateway_open_listen_streams` HPA metric (`autoscaling.listenStreams`) if
  clients hold thousands of streams.
- **Limits move with replicas:** the rate limits, the response limit and the body limit
  are per pod (ADR 0012). Ten replicas admit ten times `RATE_LIMIT_*_RPS` in total.
- **Minimum 3 replicas, PDB `minAvailable: 2`,** spread across zones (topology spread) and
  preferably across nodes (soft podAntiAffinity).
- **Rollouts:** use `maxSurge: 1, maxUnavailable: 0`. The shutdown budget is the preStop
  sleep (5 s, which lets the router drop the endpoint), then the drain (readiness 503,
  listen streams closed), then up to `SHUTDOWN_GRACE_SEC` (45 s) for in-flight calls. All
  of it must fit in `terminationGracePeriodSeconds` (60 s). Upstream calls longer than
  45 s are cancelled during a rollout; raise both values together if your tools run longer.
