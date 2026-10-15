#!/usr/bin/env bash
# Local/CI validation of everything under deploy/ (make deploy-check):
#   1. helm lint for both charts, every values overlay
#   2. helm template -> deploy/.render/<chart>-<env>.yaml
#   3. kubeconform (strict) against core schemas + the CRD cache (kubeconform/fetch-schemas.sh)
#   4. conftest restricted-SCC policy (deploy/policy) on every render and on deploy/openshift
#   5. kube-score (advisory unless KUBE_SCORE_STRICT=1) when installed
# Needs: helm, kubeconform, conftest (brew install helm kubeconform conftest).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
render="$here/.render"
schemas="$here/kubeconform/cache"
K8S_VERSION="${K8S_VERSION:-1.32.0}" # OpenShift 4.19
NAMESPACE=mcp-gateway

for tool in helm kubeconform conftest; do
  command -v "$tool" >/dev/null || { echo "missing $tool (brew install helm kubeconform conftest)"; exit 1; }
done
rm -rf "$render" && mkdir -p "$render"

# env -> values overlays layered on values.yaml (bash 3.2 on macOS: no associative arrays)
values_for() {
  case "$1" in
    default) echo "" ;;
    dev) echo "values-dev.yaml" ;;
    staging) echo "values-prod.yaml values-staging.yaml" ;;
    prod) echo "values-prod.yaml" ;;
  esac
}
# Fills the global array `args` with -f flags for chart+env (skips overlays a chart lacks;
# the repo path may contain spaces, so no word-splitting of command substitutions).
values_args() {
  local chart="$1" env="$2" f
  args=()
  for f in $(values_for "$env"); do [ -f "$here/helm/$chart/$f" ] && args+=(-f "$here/helm/$chart/$f"); done
  return 0
}
charts="mcp-gateway mcp-admin"
envs="default dev staging prod"

echo "== helm lint"
for chart in $charts; do
  for env in $envs; do
    values_args "$chart" "$env"
    helm lint --strict --quiet "$here/helm/$chart" ${args[@]+"${args[@]}"}
  done
done

echo "== helm template"
for chart in $charts; do
  for env in $envs; do
    values_args "$chart" "$env"
    helm template "$chart" "$here/helm/$chart" -n "$NAMESPACE" ${args[@]+"${args[@]}"} > "$render/$chart-$env.yaml"
    printf '  %-32s %3d objects\n' "$chart-$env.yaml" "$(grep -c '^kind:' "$render/$chart-$env.yaml")"
  done
done

echo "== kubeconform (k8s $K8S_VERSION + CRD cache)"
"$here/kubeconform/fetch-schemas.sh" "$schemas" >/dev/null
kc_args=(-strict -summary -kubernetes-version "$K8S_VERSION"
  -schema-location default
  -schema-location "$schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
kubeconform "${kc_args[@]}" "$render"/*.yaml
# OperatorGroup/Subscription (OLM) have no public schema: skip those two kinds only.
kubeconform "${kc_args[@]}" -skip OperatorGroup,Subscription \
  "$here"/openshift/namespaces "$here"/openshift/mongodb "$here"/openshift/argocd "$here"/openshift/tekton "$here"/openshift/tekton/tasks

echo "== conftest (deploy/policy)"
for f in "$render"/*.yaml; do
  case "$f" in *-dev.yaml|*-default.yaml) data="$here/policy/data-dev.yaml" ;; *) data="$here/policy/data-prod.yaml" ;; esac
  conftest test --policy "$here/policy" --data "$data" --no-color "$f"
done
conftest test --policy "$here/policy" --data "$here/policy/data-dev.yaml" --no-color \
  "$here"/openshift/namespaces "$here"/openshift/mongodb "$here"/openshift/argocd "$here"/openshift/tekton
conftest verify --policy "$here/policy" --no-color

if command -v kube-score >/dev/null; then
  echo "== kube-score (advisory)"
  # Ignored: container-security-context-user-group-id (it wants runAsUser >= 10000; OpenShift
  # assigns the UID and the restricted SCC forbids setting one), container-image-pull-policy
  # (IfNotPresent with a digest is fine), ephemeral-storage (not a concern here).
  ks_args=(score --output-format ci --kubernetes-version "v$(cut -d. -f1-2 <<<"$K8S_VERSION")"
    --ignore-test container-security-context-user-group-id,container-image-pull-policy,container-ephemeral-storage-request-and-limit)
  if [ "${KUBE_SCORE_STRICT:-0}" = "1" ]; then kube-score "${ks_args[@]}" "$render"/*.yaml
  else kube-score "${ks_args[@]}" "$render"/*.yaml | grep -Ev '^\[(OK|SKIPPED)\]' | sort -u || true; fi
fi
echo "deploy-check OK"
