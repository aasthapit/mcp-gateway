# MongoDB for the policy store (Percona Operator)

The gateway needs a **replica set** (change streams) with majority writes. In prod it is a
3-member `PerconaServerMongoDB` managed by the Percona Operator for MongoDB, Red Hat
certified and installed from OperatorHub ([ADR 0010](../../../docs/adr/0010-openshift-packaging.md)).

## Install

1. Operator (namespace-scoped, manual approval so upgrades are reviewed):
   ```bash
   oc apply -f deploy/openshift/mongodb/operator-subscription.yaml
   oc get installplan -n mcp-gateway            # approve: oc patch installplan <name> -n mcp-gateway --type merge -p '{"spec":{"approved":true}}'
   oc get csv -n mcp-gateway -w                 # percona-server-mongodb-operator-certified.v1.23.1  Succeeded
   ```
   Or through the console: OperatorHub → "Percona Operator for MongoDB" (Certified) → install
   into `mcp-gateway`.
2. Application user passwords: `app-users-externalsecret.yaml` (prod, via ESO) or
   `app-users-secret.example.yaml` (dev, edit the passwords). The operator creates the
   system-users Secret (`mcp-mongo-system-users`) itself when it does not exist.
3. Backup credentials: the same ExternalSecret file creates `mcp-mongo-backup-s3`
   (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`). Set the bucket, region and endpoint
   placeholders in `psmdb-cluster.yaml` (`spec.backup.storages.object-storage.s3`).
4. Cluster:
   ```bash
   oc apply -n mcp-gateway -f deploy/openshift/mongodb/psmdb-cluster.yaml
   oc get psmdb mcp-mongo -n mcp-gateway -w     # ready after ~3-5 min
   oc get secret mcp-mongo-ssl -n mcp-gateway   # CA for tlsCAFile= (mounted by the charts)
   ```

## What the CR sets

- `replsets[0]`: `rs0`, size 3, host anti-affinity + zone spread, PDB `maxUnavailable: 1`,
  2 GiB memory, 20 GiB PVC (set `storageClassName` to the cluster's SSD class), oplog 2 GiB
  with `oplogMinRetentionHours: 24` so a gateway replica can resume its change stream after
  a restart (PLAN §3.3). `sharding.enabled: false`.
- `tls.mode: requireTLS` with operator-issued certificates (cert-manager if present).
- `users`: SCRAM users managed by the operator (>= 1.16):
  | user        | roles                       | used by                           |
  | ----------- | --------------------------- | --------------------------------- |
  | `gateway`   | `read` on `mcp_gateway`     | gateway pods (reads + change streams; `read` includes the `changeStream` action) |
  | `admin-api` | `readWrite` on `mcp_gateway`| admin API (CRUD, version bump, history) |
- `backup`: PBM 2.15, daily logical + weekly physical to S3-compatible object storage,
  PITR with 10-minute oplog slices, 14/8 copies kept.

Connection strings (in-cluster):

```text
mongodb://gateway:<pw>@mcp-mongo-rs0.mcp-gateway.svc.cluster.local:27017/mcp_gateway?replicaSet=rs0&tls=true&tlsCAFile=/etc/gateway/mongo-ca/ca.crt
mongodb://admin-api:<pw>@mcp-mongo-rs0.mcp-gateway.svc.cluster.local:27017/mcp_gateway?replicaSet=rs0&tls=true&tlsCAFile=/etc/admin/mongo-ca/ca.crt
```

The `mcp-mongo-rs0` Service resolves to all members; the driver discovers the primary.

## Day 2

- Schema validators and seed data: run the repo's `local/mongo-init/validators.js` once
  against the cluster (`mongosh "<admin-api uri>" local/mongo-init/validators.js`); the
  admin API (WP5) owns them afterwards.
- Restore: create a `PerconaServerMongoDBRestore` pointing at a backup name or a PITR
  timestamp (operator docs "Restore from a backup").
- Version upgrades: bump `spec.image`/`crVersion` in Git; `upgradeOptions.apply: disabled`
  keeps the operator from auto-upgrading.
- Monitoring: PMM is off; the operator exposes a `mongodb_exporter`-compatible sidecar when
  `pmm.enabled` or a custom sidecar is set. Alerts worth having: replica set without a
  primary, oplog window < 6 h, backup not run in 36 h.

## Alternatives

- **MongoDB Controllers for Kubernetes (MCK)** (`MongoDBCommunity` CRD, v1.13, OpenShift
  4.21+): needs `MANAGED_SECURITY_CONTEXT` on OpenShift, no certified OperatorHub entry for
  the community edition, no built-in backups without an Enterprise entitlement (Ops Manager).
  Choose it if the platform already runs MCK with Enterprise.
- **MongoDB Atlas** (M10+): managed, change streams supported with per-tier caps; the
  gateway then needs `networkPolicy.egress.mongo.ipBlocks` (or a private endpoint) and the
  URI from the Atlas secret in the secret store; `mongoCA.enabled: false` (public CA).
- **Dev/test**: the single-node replica set container in `local/compose.yaml`.
