# ADR 0010: OpenShift packaging (Helm via Argo CD, Routes, Percona operator)

Status: accepted (WP6, 2026-10-15).

## Context

PLAN §3.6 asks for N stateless gateway replicas on OpenShift under the restricted SCC, with
TLS ingress that tolerates long tool calls and SSE streams, a MongoDB replica set for the
policy store, secrets that never live in Git, and a GitOps path from a built image to a
running deployment. Several choices were open: packaging format, ingress API, Mongo
operator, CI engine. No cluster was available to this work package; everything was
validated by rendering (`make deploy-check`) and by running the image locally under the
same constraints the chart imposes.

## Decisions

1. **Helm charts rendered by Argo CD (OpenShift GitOps)**, one chart per deployable
   (`mcp-gateway`, `mcp-admin`) with `values-{dev,staging,prod}.yaml` overlays.
   Helm because the hygiene (SCC fields, probes, NetworkPolicy, PDB, HPA) is the same in
   every environment and only parameters change; Argo CD because the platform standardises
   on it and it gives sync history, drift detection and rollback without a Helm release
   secret in the namespace. Kustomize overlays were rejected: they would repeat the full
   Deployment per environment. An `ApplicationSet` generates both charts for every
   environment from one list.
2. **Routes, not Gateway API.** Gateway API on OpenShift is GA since 4.19 but requires
   Service Mesh 3 as the implementation. Routes are built in, support the two annotations
   we need (`haproxy.router.openshift.io/timeout: 1h` for long calls,
   `disable_cookies: "true"` for no affinity) and edge/re-encrypt TLS. SSE streams need no
   router tuning because the gateway pings every 15 s (ADR 0004). Passthrough is forbidden
   by policy since it hides HTTP from the router. Revisit if the platform adopts OSSM 3.
3. **Percona Operator for MongoDB** for the policy store: Red Hat certified, on
   OperatorHub, UBI-based, replica set with `sharding.enabled: false`, operator-managed TLS,
   PBM backups with PITR, SCRAM users declared in the CR (`gateway` read-only, `admin-api`
   readWrite). MCK needs an Enterprise entitlement for backups; the community operator is
   archived; Atlas is an acceptable managed alternative and the chart supports it through
   `networkPolicy.egress.mongo.ipBlocks`.
4. **External Secrets Operator** produces every Secret in prod (`ExternalSecret` objects in
   the charts, `ClusterSecretStore vault-backend`). The chart has a plain-Secret fallback for
   dev only; it refuses to render it with `config.ENV=prod`, and the conftest policy flags
   any chart-rendered Secret in a prod render.
5. **Restricted SCC as policy, not convention.** `deploy/policy/restricted-scc.rego`
   encodes the rules (`runAsNonRoot`, no `runAsUser`, no privilege escalation, drop `ALL`,
   `RuntimeDefault` seccomp, read-only root with `emptyDir` on `/tmp`, memory limit ==
   request, probes, `automountServiceAccountToken: false`, no `:latest`) and runs on every
   render in CI. kube-score is advisory because its UID check contradicts OpenShift's
   arbitrary-UID model.
6. **Tekton (OpenShift Pipelines) for the image pipeline**: test → buildah build and push
   to a candidate tag in the internal registry → Trivy scan (HIGH/CRITICAL fail) → skopeo
   copy to the release registry preserving the digest → `yq` writes `image.tag` and
   `image.digest` into `values-prod.yaml` on the `release` branch → Argo CD syncs. The
   scan runs before anything is promoted, and the deployed reference is always a digest.
   GitHub Actions runs the repository checks (`make deploy-check`); it could run the same
   build if the cluster's internal registry is not reachable from GitHub.
7. **Schema validation with kubeconform** uses pre-converted CRD schemas from
   `datreeio/CRDs-catalog` where they exist, and converts the Route and Tekton CRDs
   itself (openshift/api `release-4.19`, Tekton `v1.17.0`). The cache is not committed
   (1-2 MB per schema); the fetch is deterministic and pinned.

## Consequences

- A chart change is validated in seconds without a cluster; the first live deployment still
  has to confirm the Route/SSE behaviour and the HPA custom metric wiring (WP7 drills).
- `mcp-admin` is a skeleton: WP5 only sets `api.image`, `ui.image`, `api.env` and the
  readiness/liveness paths.
- The re-encrypt Route option exists but needs a TLS listener in the gateway pod (not in
  WP1's image); edge TLS is the default.
- Operator and schema versions are pinned in files (`crVersion: 1.23.1`, Tekton 1.17,
  OCP 4.19 schemas); bump them deliberately.
