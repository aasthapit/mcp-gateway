# Restricted SCC (restricted-v2) and workload-hygiene rules for everything the charts render.
# Run: conftest test --policy deploy/policy <rendered.yaml>     (make deploy-policy)
# Written for OPA 1.x / conftest 0.6x (rego.v1 keywords).
package main

import rego.v1

workload_kinds := {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

is_workload if workload_kinds[input.kind]

pod_spec := input.spec.template.spec if {
	input.kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
}

pod_spec := input.spec.jobTemplate.spec.template.spec if input.kind == "CronJob"

containers contains c if {
	is_workload
	some c in pod_spec.containers
}

containers contains c if {
	is_workload
	some c in pod_spec.initContainers
}

name := sprintf("%s/%s", [input.kind, input.metadata.name])

# --- restricted SCC -------------------------------------------------------------------------

deny contains msg if {
	is_workload
	not pod_spec.securityContext.runAsNonRoot == true
	msg := sprintf("%s: pod securityContext.runAsNonRoot must be true", [name])
}

deny contains msg if {
	is_workload
	pod_spec.securityContext.runAsUser
	msg := sprintf("%s: do not set pod securityContext.runAsUser (OpenShift assigns a UID from the namespace range)", [name])
}

deny contains msg if {
	some c in containers
	c.securityContext.runAsUser
	msg := sprintf("%s: container %s must not set runAsUser", [name, c.name])
}

deny contains msg if {
	some c in containers
	not c.securityContext.allowPrivilegeEscalation == false
	msg := sprintf("%s: container %s must set allowPrivilegeEscalation: false", [name, c.name])
}

deny contains msg if {
	some c in containers
	c.securityContext.privileged == true
	msg := sprintf("%s: container %s must not be privileged", [name, c.name])
}

deny contains msg if {
	some c in containers
	not "ALL" in object.get(c, ["securityContext", "capabilities", "drop"], [])
	msg := sprintf("%s: container %s must drop ALL capabilities", [name, c.name])
}

deny contains msg if {
	some c in containers
	count(object.get(c, ["securityContext", "capabilities", "add"], [])) > 0
	msg := sprintf("%s: container %s must not add capabilities", [name, c.name])
}

deny contains msg if {
	some c in containers
	not c.securityContext.readOnlyRootFilesystem == true
	msg := sprintf("%s: container %s must set readOnlyRootFilesystem: true", [name, c.name])
}

seccomp_ok(c) if pod_spec.securityContext.seccompProfile.type == "RuntimeDefault"

seccomp_ok(c) if c.securityContext.seccompProfile.type == "RuntimeDefault"

deny contains msg if {
	some c in containers
	not seccomp_ok(c)
	msg := sprintf("%s: container %s needs seccompProfile.type RuntimeDefault (pod or container level)", [name, c.name])
}

deny contains msg if {
	is_workload
	some field in ["hostNetwork", "hostPID", "hostIPC"]
	pod_spec[field] == true
	msg := sprintf("%s: %s is not allowed", [name, field])
}

deny contains msg if {
	is_workload
	some v in pod_spec.volumes
	v.hostPath
	msg := sprintf("%s: hostPath volume %s is not allowed", [name, v.name])
}

# --- workload hygiene -----------------------------------------------------------------------

deny contains msg if {
	is_workload
	not pod_spec.automountServiceAccountToken == false
	msg := sprintf("%s: set automountServiceAccountToken: false on the pod", [name])
}

deny contains msg if {
	input.kind == "ServiceAccount"
	not input.automountServiceAccountToken == false
	msg := sprintf("%s: set automountServiceAccountToken: false", [name])
}

deny contains msg if {
	some c in containers
	some r in ["cpu", "memory"]
	not c.resources.requests[r]
	msg := sprintf("%s: container %s needs resources.requests.%s", [name, c.name, r])
}

deny contains msg if {
	some c in containers
	not c.resources.limits.memory
	msg := sprintf("%s: container %s needs resources.limits.memory", [name, c.name])
}

deny contains msg if {
	some c in containers
	c.resources.limits.memory != c.resources.requests.memory
	msg := sprintf("%s: container %s memory limit (%v) must equal the request (%v)", [name, c.name, c.resources.limits.memory, c.resources.requests.memory])
}

deny contains msg if {
	is_workload
	some c in pod_spec.containers
	some probe in ["readinessProbe", "livenessProbe"]
	not c[probe]
	msg := sprintf("%s: container %s needs a %s", [name, c.name, probe])
}

deny contains msg if {
	is_workload
	not pod_spec.terminationGracePeriodSeconds
	msg := sprintf("%s: set terminationGracePeriodSeconds explicitly", [name])
}

deny contains msg if {
	some c in containers
	endswith(c.image, ":latest")
	msg := sprintf("%s: container %s uses a :latest image", [name, c.name])
}

deny contains msg if {
	some c in containers
	not contains(c.image, "@sha256:")
	not regex.match(`:[A-Za-z0-9_.-]+$`, c.image)
	msg := sprintf("%s: container %s image %q has no tag or digest", [name, c.name, c.image])
}

deny contains msg if {
	input.kind == "Service"
	input.spec.type in {"NodePort", "LoadBalancer"}
	msg := sprintf("%s: expose through a Route, not a %s Service", [name, input.spec.type])
}

# --- Routes ---------------------------------------------------------------------------------

deny contains msg if {
	input.kind == "Route"
	not input.spec.tls.termination in {"edge", "reencrypt"}
	msg := sprintf("%s: Route must terminate TLS with edge or reencrypt", [name])
}

deny contains msg if {
	input.kind == "Route"
	not input.spec.tls.insecureEdgeTerminationPolicy in {"Redirect", "None"}
	msg := sprintf("%s: insecureEdgeTerminationPolicy must be Redirect or None", [name])
}

# The gateway Route carries long tool calls and SSE streams and must not pin clients to a pod.
deny contains msg if {
	input.kind == "Route"
	input.metadata.labels["app.kubernetes.io/name"] == "mcp-gateway"
	not input.metadata.annotations["haproxy.router.openshift.io/timeout"]
	msg := sprintf("%s: gateway Route needs haproxy.router.openshift.io/timeout (docs/adr/0004)", [name])
}

deny contains msg if {
	input.kind == "Route"
	input.metadata.labels["app.kubernetes.io/name"] == "mcp-gateway"
	input.metadata.annotations["haproxy.router.openshift.io/disable_cookies"] != "true"
	msg := sprintf("%s: gateway Route needs haproxy.router.openshift.io/disable_cookies: \"true\"", [name])
}

# --- Secrets must not be rendered from values in prod --------------------------------------

deny contains msg if {
	input.kind == "Secret"
	input.metadata.labels["app.kubernetes.io/managed-by"] == "Helm"
	data.prod_render == true
	msg := sprintf("%s: plain Secret rendered by the chart in a prod render; use ExternalSecrets", [name])
}
