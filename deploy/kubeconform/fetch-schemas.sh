#!/usr/bin/env bash
# Fetch the JSON schemas kubeconform needs for the CRDs we render (Route, ExternalSecret,
# ServiceMonitor, PerconaServerMongoDB, Argo CD Application/ApplicationSet, Tekton) into
# deploy/kubeconform/cache/<group>/<kind>_<version>.json (gitignored: 1-2 MB each).
#
# Sources:
#   - datreeio/CRDs-catalog (pre-converted with kubeconform's openapi2jsonschema.py) for
#     ExternalSecret, ServiceMonitor, PerconaServerMongoDB, Application, ApplicationSet.
#   - openshift/api (route/v1 CRD) and tektoncd/pipeline (release.yaml CRDs): no catalog
#     entry, so the CRD openAPIV3Schema is converted here with the same rules as
#     openapi2jsonschema.py (int-or-string -> oneOf, null keys dropped; no additionalProperties).
# Core Kubernetes schemas are not cached: kubeconform downloads them from
# yannh/kubernetes-json-schema on demand (-kubernetes-version in deploy-check.sh).
#
# Pins (bump deliberately):
set -euo pipefail
CATALOG_REF="${CATALOG_REF:-main}"
OPENSHIFT_API_REF="${OPENSHIFT_API_REF:-release-4.19}"
TEKTON_VERSION="${TEKTON_VERSION:-v1.17.0}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cache="${1:-$here/cache}"
mkdir -p "$cache"

fetch() { # url dest
  if [ -s "$2" ]; then return; fi
  mkdir -p "$(dirname "$2")"
  echo "fetch $1"
  curl -fsSL --retry 3 -o "$2.tmp" "$1" && mv "$2.tmp" "$2"
}

catalog="https://raw.githubusercontent.com/datreeio/CRDs-catalog/${CATALOG_REF}"
for f in external-secrets.io/externalsecret_v1.json \
         monitoring.coreos.com/servicemonitor_v1.json \
         psmdb.percona.com/perconaservermongodb_v1.json \
         argoproj.io/application_v1alpha1.json \
         argoproj.io/applicationset_v1alpha1.json; do
  fetch "$catalog/$f" "$cache/$f"
done

# CRDs that need converting.
crds="$cache/.crds"
fetch "https://raw.githubusercontent.com/openshift/api/${OPENSHIFT_API_REF}/route/v1/zz_generated.crd-manifests/routes.crd.yaml" "$crds/routes.crd.yaml"
fetch "https://github.com/tektoncd/pipeline/releases/download/${TEKTON_VERSION}/release.yaml" "$crds/tekton-release.yaml"

if [ ! -s "$cache/route.openshift.io/route_v1.json" ] || [ ! -s "$cache/tekton.dev/pipeline_v1.json" ]; then
  if python3 -c 'import yaml' 2>/dev/null; then PY=(python3 -I); else PY=(uv run --no-project --with pyyaml python -I); fi
  "${PY[@]}" - "$cache" "$crds/routes.crd.yaml" "$crds/tekton-release.yaml" <<'PY'
import json, os, sys, yaml

out = sys.argv[1]

def int_or_string(node):
    """openapi2jsonschema.py rules plus two CRD quirks: `x-kubernetes-int-or-string: true`
    (OpenAPI v3 spelling, e.g. Route spec.port.targetPort) becomes oneOf string/integer, and
    null-valued keys (openshift/api emits `anyOf: null`) are dropped: they break the compiler."""
    if isinstance(node, dict):
        if node.get("format") == "int-or-string" or node.get("x-kubernetes-int-or-string") is True:
            keep = {k: v for k, v in node.items() if k in ("description", "default")}
            return {**keep, "oneOf": [{"type": "string"}, {"type": "integer"}]}
        return {k: int_or_string(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [int_or_string(x) for x in node]
    return node

for path in sys.argv[2:]:
    with open(path) as f:
        for doc in yaml.safe_load_all(f):
            if not doc or doc.get("kind") != "CustomResourceDefinition":
                continue
            group = doc["spec"]["group"]
            kind = doc["spec"]["names"]["kind"].lower()
            for v in doc["spec"]["versions"]:
                schema = v.get("schema", {}).get("openAPIV3Schema")
                if not schema:
                    continue
                schema = int_or_string(schema)
                schema["$schema"] = "http://json-schema.org/schema#"
                dest = os.path.join(out, group, f"{kind}_{v['name']}.json")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w") as o:
                    json.dump(schema, o, indent=1)
                print("wrote", os.path.relpath(dest, out))
PY
fi
echo "schemas in $cache"
