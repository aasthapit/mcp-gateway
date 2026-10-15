# deploy — OpenShift packaging (WP6)

Everything needed to run the gateway on OpenShift 4.16+ without reading the chart source:
Helm charts for the gateway and the admin API/UI, the MongoDB cluster (Percona operator),
Argo CD applications, a Tekton build pipeline, and the local validation that CI runs.
Design: [docs/PLAN.md §3.6](../docs/PLAN.md), decisions: [ADR 0010](../docs/adr/0010-openshift-packaging.md).

| Path                    | What                                                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `helm/mcp-gateway/`     | Gateway chart: Deployment, Service, Route, HPA, PDB, NetworkPolicy, SA, ConfigMap, ExternalSecret/Secret, ServiceMonitor |
| `helm/mcp-admin/`       | Admin API + UI chart skeleton (WP5 sets images and env)                                                                  |
| `openshift/namespaces/` | `mcp-gateway` and `mcp-upstreams` namespaces (PSA restricted) + upstream ingress policy                                  |
| `openshift/mongodb/`    | Percona `PerconaServerMongoDB` CR (3-member replica set, TLS, backups, SCRAM users) + operator subscription              |
| `openshift/argocd/`     | Argo CD `Application` (prod) and `ApplicationSet` (dev/staging/prod x both charts)                                       |
| `openshift/tekton/`     | Pipeline: test, build (buildah), scan (Trivy), promote by digest (skopeo), bump values (yq + git)                        |
| `policy/`               | conftest (Rego) rules for the restricted SCC and workload hygiene, with unit tests                                       |
| `kubeconform/`          | CRD schema fetcher for kubeconform (cache is gitignored)                                                                 |
| `deploy-check.sh`       | `make deploy-check`: lint, template, kubeconform, conftest, kube-score                                                   |

## Prerequisites (cluster)

- OpenShift 4.16+ (tested renders against the Kubernetes 1.32 schemas of OCP 4.19).
- Operators from OperatorHub: **Percona Operator for MongoDB** (certified), **External Secrets
  Operator for Red Hat OpenShift** (prod), **OpenShift GitOps** (Argo CD), **OpenShift
  Pipelines** (Tekton, only for the build), **user workload monitoring** enabled for the
  ServiceMonitor.
- A `ClusterSecretStore` named `vault-backend` (or change `externalSecrets.secretStoreRef`).
- Namespace-level access to `mcp-gateway`; `oc apply` of the namespaces needs cluster-admin.

Local tools for validation: `brew install helm kubeconform conftest kube-score`.

## First install (dev cluster, by hand)

```bash
oc apply -f deploy/openshift/namespaces/namespaces.yaml
oc apply -f deploy/openshift/mongodb/operator-subscription.yaml      # then approve the InstallPlan
oc get csv -n mcp-gateway -w                                          # wait for Succeeded
# app users: dev = plain Secret (edit the passwords first), prod = ExternalSecret
oc apply -n mcp-gateway -f deploy/openshift/mongodb/app-users-secret.example.yaml
oc apply -n mcp-gateway -f deploy/openshift/mongodb/psmdb-cluster.yaml
oc get psmdb -n mcp-gateway -w                                        # STATUS ready (3-5 min)

helm upgrade --install mcp-gateway deploy/helm/mcp-gateway -n mcp-gateway \
  -f deploy/helm/mcp-gateway/values-dev.yaml \
  --set route.host=mcp-dev.apps.<cluster-domain> \
  --set config.OIDC_ISSUER=https://<idp>/realms/mcp \
  --set secrets.values.mongoUri='mongodb://gateway:<pw>@mcp-mongo-rs0.mcp-gateway.svc.cluster.local:27017/mcp_gateway?replicaSet=rs0&tls=true&tlsCAFile=/etc/gateway/mongo-ca/ca.crt' \
  --set secrets.values.internalToken="$(openssl rand -hex 24)"

helm upgrade --install mcp-admin deploy/helm/mcp-admin -n mcp-gateway \
  -f deploy/helm/mcp-admin/values-dev.yaml --set route.host=mcp-admin-dev.apps.<cluster-domain> \
  --set api.env.ADMIN_OIDC_ISSUER=https://<idp>/realms/mcp --set ui.env.ADMIN_UI_OIDC_AUTHORITY=https://<idp>/realms/mcp \
  --set secrets.values.mongoUri='mongodb://admin-api:<pw>@mcp-mongo-rs0.mcp-gateway.svc.cluster.local:27017/mcp_gateway?replicaSet=rs0&tls=true&tlsCAFile=/etc/admin/mongo-ca/ca.crt'
```

Release names matter: `mcp-gateway` and `mcp-admin` make the Service names match what the
admin chart's `ADMIN_GATEWAY_INTERNAL_URL`, its `internalToken.secretName` (`mcp-gateway-env`)
and the NetworkPolicy selectors expect. The IdP needs a public PKCE client `mcp-admin-ui`
(redirect `https://<admin host>/*`) and tokens with `aud=mcp-admin` for admins (see
`local/keycloak/realm.json`: client role `mcp-admin/policy-admin`, composite of the realm role).

Check:

```bash
oc rollout status deploy/mcp-gateway -n mcp-gateway
curl -s https://mcp-dev.apps.<cluster-domain>/readyz          # 200 once Mongo + upstreams answer
cd gateway && uv run python -m mcp_gateway.smoke https://mcp-dev.apps.<cluster-domain>
```

## Production (GitOps)

Prod is never installed by hand. The pipeline writes an immutable digest into
`helm/mcp-gateway/values-prod.yaml` on the `release` branch, Argo CD renders the chart:

```bash
oc apply -n openshift-gitops -f deploy/openshift/argocd/applicationset.yaml   # or application-mcp-gateway.yaml
```

Prod values (`values-prod.yaml`): ESO-managed Secrets, `route.host`, digest-pinned image,
HPA 3..10, PDB `minAvailable: 2`, ServiceMonitor, OTel export, topology spread across zones.
Edit the `example.com` hosts, the IdP issuer and the `ClusterSecretStore` name before the first
sync; the prod render fails on purpose if `route.host` is empty (the gateway refuses a
non-https `GATEWAY_PUBLIC_BASE_URL` in prod).

## Upgrade, rollback, scaling

| Operation      | Dev (helm)                                                                   | Prod (Argo CD)                                                                      |
| -------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| New image      | `helm upgrade ... --set image.tag=<tag>`                                     | Pipeline bumps `image.digest`; Argo CD syncs (auto or manual)                       |
| Config change  | edit values, `helm upgrade` (ConfigMap checksum triggers a rolling restart)  | commit to the watched branch                                                        |
| Rollback       | `helm history mcp-gateway -n mcp-gateway && helm rollback mcp-gateway <rev>` | `git revert` the values bump (Argo CD syncs back) or Argo UI "History and rollback" |
| Scale          | `--set replicaCount=N` (HPA off) or `--set autoscaling.minReplicas=N`        | same keys in `values-prod.yaml`                                                     |
| Pause rollouts | `oc rollout pause deploy/mcp-gateway`                                        | disable `automated` sync on the Application                                         |

Rolling update is `maxSurge: 1, maxUnavailable: 0`; the PDB keeps 2 pods during node drains.
Each pod runs one uvicorn worker (one change stream, one upstream pool): scale with replicas,
never with workers. Pods take up to 60 s to drain (`terminationGracePeriodSeconds`), with a
5 s preStop sleep so the router stops sending traffic before SIGTERM.

HPA scales on CPU (70 % by default). `autoscaling.listenStreams.enabled: true` adds the
custom metric `gateway_open_listen_streams` (pods metric), which needs the Prometheus
adapter (or Custom Metrics Autoscaler / KEDA) to expose it; a rule for the
`prometheus-adapter` looks like:

```yaml
rules:
  - seriesQuery: 'gateway_open_listen_streams{namespace!="",pod!=""}'
    resources: { overrides: { namespace: { resource: namespace }, pod: { resource: pod } } }
    name: { matches: "gateway_open_listen_streams", as: "gateway_open_listen_streams" }
    metricsQuery: "sum(<<.Series>>{<<.LabelMatchers>>}) by (<<.GroupBy>>)"
```

## Route timeout and TLS

The gateway Route carries two long-lived things: tool calls that may take minutes and
`subscriptions/listen` SSE streams. `haproxy.router.openshift.io/timeout: 1h` raises the
router's 30 s idle timeout for the tool calls. The SSE streams themselves need no extra help:
FastMCP writes a `: ping` comment every 15 s on every open stream
([ADR 0004](../docs/adr/0004-sse-keepalive.md)), so they never go idle from the router's
point of view. `disable_cookies: "true"` stops the router from pinning a client to one pod;
the gateway is stateless and every replica serves every request.

TLS is `edge` by default (router terminates TLS, plain HTTP inside the cluster's overlay).
`reencrypt` is wired (`route.tls.termination: reencrypt`, `destinationCACertificate`) but the
gateway image only listens on plain HTTP today; use it once the pod terminates TLS itself
(a service-serving-cert on the Service plus a TLS listener or a TLS sidecar). Passthrough is
rejected by the policy: the router cannot see HTTP, so the timeout annotation and
`insecureEdgeTerminationPolicy: Redirect` stop working.

## Secrets flow (External Secrets Operator)

Nothing secret lives in Git or in Helm values in prod. The chart renders `ExternalSecret`
objects; ESO materialises Kubernetes Secrets from the configured store and refreshes them hourly.

```text
Vault / cloud secret manager ──(ClusterSecretStore vault-backend)──▶ ESO ──▶ Secret ──▶ pod
  mcp-gateway/mongo      uri          ─▶ <release>-env        GATEWAY_MONGO_URI        envFrom
  mcp-gateway/internal   token        ─▶ <release>-env        GATEWAY_INTERNAL_TOKEN   envFrom
  mcp-gateway/upstreams  *            ─▶ <release>-upstreams  one file per key          /etc/gateway/upstream-secrets
  mcp-admin/mongo        uri          ─▶ <release>-api-env    ADMIN_MONGO_URI          envFrom (admin chart)
  (gateway's Secret)     GATEWAY_INTERNAL_TOKEN ─▶ mcp-gateway-env ─▶ ADMIN_GATEWAY_INTERNAL_TOKEN  secretKeyRef (admin chart)
  mcp-gateway/mongo-users ...         ─▶ mcp-mongo-app-users  Percona spec.users passwords
  mcp-gateway/mongo-backup ...        ─▶ mcp-mongo-backup-s3  PBM object-storage credentials
```

The remote keys/properties are values (`externalSecrets.env.data[*].remoteRef`), so a site
with a different layout only edits values. The upstream credentials Secret is mounted as
files (0440) at `GATEWAY_UPSTREAM_SECRETS_DIR`; WP4 reads `<file>` per upstream. The
Percona operator's own CA (`mcp-mongo-ssl`, key `ca.crt`) is mounted at
`/etc/gateway/mongo-ca/ca.crt` for `tlsCAFile=` in the Mongo URI. Rotating a secret: change
it in the store, wait for `refreshInterval` (or `oc annotate externalsecret ... force-sync=$(date +%s)`),
then `oc rollout restart deploy/mcp-gateway` (env vars are read at start).

Dev fallback: `externalSecrets.enabled: false` + `secrets.create: true` renders plain Secrets
from values (`values-dev.yaml`). The chart refuses that combination when `config.ENV=prod`,
and the conftest policy flags any chart-rendered Secret in a prod render.

## Network policy

Gateway pods are default-deny in both directions, then:

| Direction | From / to                                                       | Port       | Why                                    |
| --------- | --------------------------------------------------------------- | ---------- | -------------------------------------- |
| ingress   | namespace `network.openshift.io/policy-group: ingress`          | 8080       | Route traffic                          |
| ingress   | pods `mcp-admin` / `api` (same namespace)                       | 8080       | `/internal/*` catalog + preview        |
| ingress   | `openshift-monitoring`, `openshift-user-workload-monitoring`    | 9090       | Prometheus scrape                      |
| egress    | `openshift-dns`                                                 | 53, 5353   | DNS                                    |
| egress    | Percona pods (`app.kubernetes.io/name: percona-server-mongodb`) | 27017      | policy store + change stream           |
| egress    | namespace `mcp-upstreams`                                       | any        | in-cluster upstream MCP servers        |
| egress    | non-RFC1918 IPs                                                 | 443        | IdP discovery/JWKS, external upstreams |
| egress    | OTel collector namespace (prod)                                 | 4317, 4318 | traces                                 |

A HostNetwork ingress controller shows up as the `default` namespace: add it to
`networkPolicy.ingress.extraFrom`. An external Mongo (Atlas): set `networkPolicy.egress.mongo.ipBlocks`.

## Dev vs prod values

| Area         | `values-dev.yaml`                           | `values-prod.yaml`                                 |
| ------------ | ------------------------------------------- | -------------------------------------------------- |
| image        | `:dev`, `pullPolicy: Always`                | tag + immutable `digest` set by the pipeline       |
| replicas     | 1, HPA off, PDB off                         | HPA 3..10 on CPU 60 %, PDB `minAvailable: 2`       |
| secrets      | plain Secrets from values                   | ExternalSecrets from `vault-backend`               |
| route host   | router-generated                            | `mcp.example.com` (required)                       |
| GATEWAY_ENV  | `dev` (http base URL allowed, console logs) | `prod` (guards: https, JSON logs, host protection) |
| telemetry    | off                                         | `native` OTLP to `otel-collector.observability`    |
| https egress | any IP                                      | non-RFC1918 only                                   |
| monitoring   | no ServiceMonitor                           | ServiceMonitor every 30 s                          |
| spread       | best effort                                 | zone spread `DoNotSchedule`                        |

`values-staging.yaml` layers a staging IdP/host on top of `values-prod.yaml`.

## Validation (`make deploy-check`)

Runs on this Mac and in CI (`.github/workflows/ci.yml`, job `deploy-check`):

1. `helm lint --strict` for both charts and every overlay.
2. `helm template` into `deploy/.render/<chart>-<env>.yaml`.
3. `kubeconform -strict` against the Kubernetes 1.32 schemas plus CRD schemas for Route,
   ExternalSecret, ServiceMonitor, PerconaServerMongoDB, Argo CD and Tekton.
   `kubeconform/fetch-schemas.sh` downloads them into `kubeconform/cache/` (gitignored):
   pre-converted JSON from the `datreeio/CRDs-catalog`, and for Route (openshift/api
   `release-4.19`) and Tekton (`v1.17.0` release manifest) the CRD `openAPIV3Schema` is
   converted in the script with the same rules as kubeconform's `openapi2jsonschema.py`
   (plus dropping the `anyOf: null` openshift/api emits next to `x-kubernetes-int-or-string`).
   OLM `OperatorGroup`/`Subscription` have no public schema and are skipped.
4. `conftest test` with `policy/restricted-scc.rego` on every render and on `openshift/`:
   `runAsNonRoot`, no `runAsUser`, `allowPrivilegeEscalation: false`, drop `ALL`,
   `seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem`, requests/limits with memory
   limit == request, probes, `automountServiceAccountToken: false`, no `:latest`, Routes with
   edge/reencrypt TLS, the gateway Route's timeout and cookie annotations, no chart-rendered
   Secret in prod renders. `conftest verify` runs the policy's own unit tests.
5. `kube-score` (advisory; `KUBE_SCORE_STRICT=1` to fail on it).

`make deploy-image` builds `gateway/Containerfile` as `<image.repository>:<appVersion>` with
podman, the exact reference the default render uses. Verified on this Mac: the image runs
with `--read-only --cap-drop ALL --user 12345:0` and the rendered ConfigMap as env, and
answers `/healthz`, `/readyz` (200) on 8080 and `/metrics` on 9090.

## Not verified here

No OpenShift cluster was available: the charts have not been applied to a live cluster, so
the "deploys to a dev cluster with 3 replicas; SSE stream survives > 30 s through the Route"
part of the WP6 definition of done is still open. First live run: `oc apply` the namespaces,
install the operators, then follow "First install" above and run the smoke client.
