# mcp-gateway

Enterprise MCP gateway: one streamable-HTTP endpoint that proxies many upstream MCP servers,
with per-caller tool allow-lists (a guest set for anonymous callers, more for JWT bearers)
stored in MongoDB and hot-reloaded on every replica. Python 3.12 + FastMCP 4.1, Node admin
API/UI, OpenShift deployment.

**Start with [docs/PLAN.md](docs/PLAN.md)** — goals, research findings, architecture, data
model, work packages and open decisions. Research notes live in [docs/research](docs/research).

## Layout

| Directory           | What                                                           | Work package |
| ------------------- | -------------------------------------------------------------- | ------------ |
| `gateway/`          | FastMCP gateway (Python, uv)                                   | WP1–WP4, WP7 |
| `admin-api/`        | Fastify admin API                                              | WP5          |
| `admin-ui/`         | React admin UI                                                 | WP5          |
| `schemas/`          | JSON Schema source of truth → Mongo validators, ajv, pydantic  | WP0          |
| `sample-upstreams/` | Two tiny FastMCP servers for local dev and e2e                 | WP0          |
| `local/`            | compose stack: Mongo replica set, Keycloak, upstreams, seed    | WP0          |
| `deploy/`           | Helm charts, OpenShift manifests, Argo CD app, Tekton pipeline | WP6          |
| `docs/`             | plan, research, ADRs, runbook                                  | –            |

## Local development

Prerequisites: [Podman](https://podman.io) (`brew install podman podman-compose docker-compose`,
then `podman machine init && podman machine start`) **or** Docker; Python 3.12+; `uv`;
Node 22+ for the admin API/UI.

```bash
make up          # build + start Mongo (rs0), Keycloak, sample upstreams; apply validators
make seed        # load local/seed/*.json and bump meta.version
make token       # dev1 access token (KC_USER=admin1 for admin1) (aud=mcp-gateway, realm_access.roles ∋ mcp-developer)
make decode      # pretty-print its claims
make smoke       # infra, gateway (guest/dev1/live allow-list edit) and admin-API checks
make admin-dev   # admin UI http://localhost:3000 (admin1/admin1) + API :3001 + gateway :8080
make down        # stop (make clean also drops volumes)
```

`make up COMPOSE="docker compose"` selects Docker. See [local/README.md](local/README.md) for
ports, credentials and connection strings.

## Contributing

```bash
pip install pre-commit && make pre-commit   # ruff, prettier, schema freshness on commit
make lint
```

CI (`.github/workflows/ci.yml`) runs the same hooks, rebuilds the Mongo validators from
`schemas/` and fails if `local/mongo-init/validators.js` is stale, validates the seed
documents, checks `local/compose.yaml`, and runs `make deploy-check` (Helm lint/template,
kubeconform, restricted-SCC policy) for [deploy/](deploy/README.md).
