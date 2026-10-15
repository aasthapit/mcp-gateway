# deploy

OpenShift packaging (Helm charts, Percona MongoDB CR, ExternalSecrets, NetworkPolicies, Argo CD
application). Implemented in **WP6**; see [docs/PLAN.md](../docs/PLAN.md) §3.6.

- `helm/mcp-gateway/` — gateway chart + `values-{dev,staging,prod}.yaml`
- `helm/mcp-admin/` — admin API + UI chart
- `openshift/` — namespace, Percona `PerconaServerMongoDB` CR, ExternalSecrets, NetworkPolicies, Argo app
