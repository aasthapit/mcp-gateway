# Infra constraints for a horizontally-scaled MCP gateway on OpenShift (researched 2026-10-14)

Items marked **[unverified]** were not confirmed against primary sources.

## 1. MCP Streamable HTTP transport

Current spec revision is **2026-07-28** and it removed protocol-level sessions (https://modelcontextprotocol.io/specification/versioning). Changelog (https://modelcontextprotocol.io/specification/2026-07-28/changelog): "Remove protocol-level sessions and the `Mcp-Session-Id` header … Servers that need cross-call state use explicit, server-minted handles passed as ordinary tool arguments (SEP-2567)"; "Make MCP stateless: remove the `initialize`/`notifications/initialized` handshake. Every request now carries its protocol version and client capabilities in `_meta`".

Modern transport (https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http):
- Single endpoint, POST only; GET stream endpoint removed.
- Every POST MUST carry `MCP-Protocol-Version`, `Mcp-Method`, and (for tools/call, resources/read, prompts/get) `Mcp-Name`; mismatch → 400 + `-32020 HeaderMismatch`. Servers MUST implement `server/discover`.
- Notifications: `subscriptions/listen` request whose response is a long-lived SSE stream carrying opted-in types (`toolsListChanged`, …). No `Last-Event-ID` resumption. Servers encouraged to emit SSE comment keep-alives and SHOULD send `X-Accel-Buffering: no`.
- `notifications/tools/list_changed`: servers with `listChanged` SHOULD notify clients that opened `subscriptions/listen` with `toolsListChanged: true`. Subscriptions are per-stream; no state across reconnects.
- `tools/list` results require `ttlMs` and `cacheScope` freshness hints.
- Origin MUST be validated (403). "Servers SHOULD implement proper authentication for all connections."

Legacy transport (https://modelcontextprotocol.io/specification/2025-11-25/basic/transports): server MAY assign `MCP-Session-Id` at initialize; GET MUST return `text/event-stream` or 405; stateless legacy servers allowed.

Backward compatibility (https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning): dual-era server MAY implement both; modern `_meta` → stateless; `initialize` → legacy semantics. Modern-only server receiving legacy traffic SHOULD: GET/DELETE → 405; ignore `Mcp-Session-Id`, do not mint IDs.

Implications for N replicas: build stateless for both eras (no session IDs, GET → 405); no sticky sessions or shared session store; legacy clients lose push `list_changed` (must re-list); `subscriptions/listen` lands on one replica, so each pod learns of policy changes itself and fans out to its own listeners; tune route timeouts and send keep-alives.

Authorization (https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization): OPTIONAL; MCP server = OAuth 2.1 resource server; MUST implement RFC 9728 metadata; 401 carries `WWW-Authenticate: Bearer resource_metadata="…"`; tokens MUST be in the `Authorization` header, MUST NOT be in the query string; MUST validate audience (RFC 8707); MUST NOT pass through tokens to upstreams; DCR deprecated in favour of Client ID Metadata Documents; PKCE S256 required. Tool set "MAY vary by the authorization presented on the request". Serving unauthenticated clients is allowed; if the gateway never returns 401 for anonymous callers, clients won't auto-discover elevated access.

## 2. MongoDB as runtime policy store

Change streams (https://www.mongodb.com/docs/manual/changeStreams/): replica sets and sharded clusters only; majority-committed events only; resume via `resumeAfter`/`startAfter`; token must be within the oplog; drivers reconnect through another node on failover; 9.0: `InterruptedDueToReplStateChange (11602)` is resumable; pool size must exceed open streams. Oplog sizing (https://www.mongodb.com/docs/manual/core/replica-set-oplog/): `storage.oplogMinRetentionHours`.

Pattern: one change stream per pod + periodic poll (30–60 s) of a `config_meta` version document; on event or bump, reload the full snapshot and emit `list_changed` to local listeners; resume on error, full reload on `ChangeStreamHistoryLost`. Atlas caps open change streams (Flex: 10; M10/M20: 100) per https://www.mongodb.com/docs/atlas/reference/flex-limitations/. **[unverified]** Atlas Flex change-stream support (docs contradict).

Read/write concerns (https://www.mongodb.com/docs/manual/reference/write-concern/): default `w: majority`; for read-your-writes use `readConcern: majority` + `w: majority`.

Schema validation (https://www.mongodb.com/docs/manual/core/schema-validation/specify-json-schema/): `$jsonSchema`, draft 4 subset (no `$ref`/`format`); `validationLevel: strict`, `validationAction: error` by default.

Operators (Oct 2026):
- MongoDB Community Operator: deprecated; archived 2025-12-12 (https://github.com/mongodb/mongodb-kubernetes-operator).
- MongoDB Controllers for Kubernetes (MCK) v1.13.0 (2026-09-29), supports OpenShift 4.21–4.23 (https://www.mongodb.com/docs/kubernetes/current/tutorial/plan-k8s-op-compatibility/); `MongoDBCommunity` CRD; OpenShift needs `MANAGED_SECURITY_CONTEXT`. **[unverified]** restricted SCC without custom grants.
- Percona Operator for MongoDB v1.23.1 (2026-10-05): Red Hat Certified, OperatorHub, OpenShift 4.19–4.22, sharding on by default (`sharding.enabled: false` for plain RS), UBI 10 base (https://docs.percona.com/percona-operator-for-mongodb/openshift.html). **[unverified]** SCC requirements.
- Atlas: managed; change streams supported with tier caps.

Recommendation: prod → Percona Operator (or MCK with Enterprise entitlement, or Atlas M10+); local → single-node replica set container.

## 3. OpenShift production deployment (Python ASGI)

SCC (https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/authentication_and_authorization/managing-pod-security-policies): `restricted-v2` drops all caps, `allowPrivilegeEscalation` false, `runAsUser: MustRunAsRange`; `restricted-v3` adds user namespaces (`hostUsers: false`) **[unverified which release made it default]**. `readOnlyRootFilesystem` not required; set it and mount `emptyDir` on `/tmp`.
Image rules (https://docs.okd.io/latest/openshift_images/create-images.html): arbitrary UID; group-0 writable dirs; numeric `USER`; no ports < 1024.
Pod spec: `runAsNonRoot: true`, no `runAsUser`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem: true`.

Base image: `registry.access.redhat.com/ubi9/python-312` (or `-minimal`) — arbitrary-UID ready, `/opt/app-root/src`, port 8080 (https://catalog.redhat.com/software/containers/ubi9/python-312/657b08d023df896ebfacf402).

Ingress for SSE: `haproxy.router.openshift.io/timeout=1h` (https://github.com/openshift/openshift-docs/blob/main/modules/nw-configuring-route-timeouts.adoc); Ingress Controller defaults `serverTimeout: 30s`, `clientTimeout: 30s`, `tunnelTimeout: 1h`. TLS edge or re-encrypt recommended; passthrough can't do cookies. Sticky-session annotations (`router.openshift.io/cookie_name`, `haproxy.router.openshift.io/disable_cookies`, `haproxy.router.openshift.io/balance`) exist but are unnecessary with a stateless design. Gateway API GA since 4.19 with OSSM 3; Routes are the lower-risk default.

Workload hygiene: ≥2–3 replicas; PDB `minAvailable`; HPA on CPU + custom metric for open SSE streams; `terminationGracePeriodSeconds` ≥ SSE close window; probes on non-MCP paths with readiness checking Mongo; requests/limits with memory limit = request; dedicated ServiceAccount, `automountServiceAccountToken: false`; default-deny NetworkPolicy.

Secrets: External Secrets Operator for Red Hat OpenShift (4.20+, 1.2.1 Sept 2026) (https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/security_and_compliance/external-secrets-operator-for-red-hat-openshift) **[unverified GA vs Tech Preview wording in errata]**.

Packaging/CD: Argo CD (OpenShift GitOps 1.18) accepts Kustomize, Helm, OCI, plain YAML; CI = OpenShift Pipelines (Tekton). Helm chart for the app rendered by Argo CD; Kustomize overlays if the platform standardises on it.

## 4. Local dev with Podman on macOS (Apple Silicon)

- `brew install podman` works (podman.io lists the pkg installer first); `podman machine init && podman machine start`. Podman 6.0 (2026-06-24): default provider `libkrun`; `--provider applehv` override; Intel removed (https://github.com/containers/podman/releases/tag/v6.0.0). Rootless by default; `podman machine set --rootful` to flip.
- `podman compose` wraps `docker-compose` (preferred if installed) or `podman-compose` (https://docs.podman.io/en/latest/markdown/podman-compose.1.html); `PODMAN_COMPOSE_WARNING_LOGS=false` silences the warning.
- Single-node replica set: `mongo` image with `--replSet rs0 --keyFile /etc/mongo-keyfile --bind_ip_all`; keyfile `openssl rand -base64 756`, `chmod 400`, owned by mongod UID; `MONGO_INITDB_ROOT_USERNAME/PASSWORD`; `rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]})` once; connect with `?replicaSet=rs0&directConnection=true` (https://www.mongodb.com/docs/manual/tutorial/deploy-replica-set-with-keyfile-access-control/).
- Gotchas: rootless can't bind host ports < 1024; `$HOME` mounted via virtiofs; re-create the machine after Podman upgrades. **[unverified]** rootful mode and ports < 1024.
- Docker Desktop 29 interim: same compose files work; licence is free only for small businesses/personal/OSS (https://docs.docker.com/subscription/desktop-license/).

## 5. JWT verification

- RFC 8725 (https://www.rfc-editor.org/rfc/rfc8725.html): explicit alg allow-list, never `none`; reject on any failed check; validate `iss`, `aud`; never fetch keys from `jku`/`x5u`; treat `kid` as untrusted; consider `typ: at+jwt`; enforce `exp`, `nbf`, small skew.
- JWKS caching (https://learn.microsoft.com/en-us/entra/identity-platform/signing-key-rollover): cache per `kid`; 24 h TTL with hourly refresh; refresh on unknown `kid` but no more than every 5 min; fetch `jwks_uri` from OIDC discovery. **[unverified]** Keycloak-specific rotation docs.
- Claims: Keycloak `realm_access.roles` / `resource_access.<client>.roles` / `scope`; Entra `roles`, `scp`, `groups`; Okta `groups`, `scp`. Normalise into an internal principal.
- Anonymous + elevated: valid per MCP. Rules: present-but-invalid token = hard 401; tokens only from `Authorization: Bearer`; validate `aud` = gateway resource URI; pin `alg` per issuer; never forward the client token upstream; treat tool-list filtering as per-request and never share cached lists across principals.
