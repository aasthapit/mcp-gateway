package main

import rego.v1

good_deployment := {
	"kind": "Deployment",
	"metadata": {"name": "ok"},
	"spec": {"template": {"spec": {
		"automountServiceAccountToken": false,
		"terminationGracePeriodSeconds": 60,
		"securityContext": {"runAsNonRoot": true, "seccompProfile": {"type": "RuntimeDefault"}},
		"containers": [{
			"name": "app",
			"image": "ghcr.io/x/app:1.0.0",
			"securityContext": {
				"allowPrivilegeEscalation": false,
				"readOnlyRootFilesystem": true,
				"capabilities": {"drop": ["ALL"]},
			},
			"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "128Mi"}},
			"readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
			"livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
		}],
	}}},
}

test_good_deployment_passes if {
	count(deny) == 0 with input as good_deployment
}

test_run_as_user_denied if {
	bad := json.patch(good_deployment, [{"op": "add", "path": "/spec/template/spec/securityContext/runAsUser", "value": 1001}])
	some msg in deny with input as bad
	contains(msg, "runAsUser")
}

test_missing_drop_all_denied if {
	bad := json.patch(good_deployment, [{"op": "replace", "path": "/spec/template/spec/containers/0/securityContext/capabilities/drop", "value": ["NET_RAW"]}])
	some msg in deny with input as bad
	contains(msg, "drop ALL")
}

test_memory_limit_mismatch_denied if {
	bad := json.patch(good_deployment, [{"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "256Mi"}])
	some msg in deny with input as bad
	contains(msg, "memory limit")
}

test_privilege_escalation_denied if {
	bad := json.patch(good_deployment, [{"op": "replace", "path": "/spec/template/spec/containers/0/securityContext/allowPrivilegeEscalation", "value": true}])
	some msg in deny with input as bad
	contains(msg, "allowPrivilegeEscalation")
}

test_writable_rootfs_denied if {
	bad := json.patch(good_deployment, [{"op": "remove", "path": "/spec/template/spec/containers/0/securityContext/readOnlyRootFilesystem"}])
	some msg in deny with input as bad
	contains(msg, "readOnlyRootFilesystem")
}

test_no_seccomp_denied if {
	bad := json.patch(good_deployment, [{"op": "remove", "path": "/spec/template/spec/securityContext/seccompProfile"}])
	some msg in deny with input as bad
	contains(msg, "seccompProfile")
}

test_automount_denied if {
	bad := json.patch(good_deployment, [{"op": "replace", "path": "/spec/template/spec/automountServiceAccountToken", "value": true}])
	some msg in deny with input as bad
	contains(msg, "automountServiceAccountToken")
}

test_latest_tag_denied if {
	bad := json.patch(good_deployment, [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "ghcr.io/x/app:latest"}])
	some msg in deny with input as bad
	contains(msg, ":latest")
}

test_route_passthrough_denied if {
	route := {"kind": "Route", "metadata": {"name": "r", "labels": {}, "annotations": {}}, "spec": {"tls": {"termination": "passthrough"}}}
	some msg in deny with input as route
	contains(msg, "edge or reencrypt")
}

test_gateway_route_needs_timeout if {
	route := {
		"kind": "Route",
		"metadata": {"name": "gw", "labels": {"app.kubernetes.io/name": "mcp-gateway"}, "annotations": {"haproxy.router.openshift.io/disable_cookies": "true"}},
		"spec": {"tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}},
	}
	some msg in deny with input as route
	contains(msg, "timeout")
}

test_nodeport_denied if {
	svc := {"kind": "Service", "metadata": {"name": "s"}, "spec": {"type": "NodePort"}}
	some msg in deny with input as svc
	contains(msg, "NodePort")
}
