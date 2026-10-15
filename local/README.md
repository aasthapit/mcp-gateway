# local

Local development stack (`compose.yaml`), driven by the root `Makefile`.

| Service          | Host port | Notes                                                                          |
| ---------------- | --------- | ------------------------------------------------------------------------------ |
| `mongo`          | 27017     | `mongo:8`, single-node replica set `rs0`, keyfile auth, root user from `.env`  |
| `mongo-init`     | –         | one-shot: `rs.initiate` + `$jsonSchema` validators; also runs `make seed`      |
| `keycloak`       | 8180      | dev mode, realm `mcp` imported from `keycloak/realm.json`, admin `admin/admin` |
| `sample-public`  | 8081      | FastMCP upstream: `time_now`, `echo`                                           |
| `sample-private` | 8082      | FastMCP upstream: `secret_lookup`                                              |
| `gateway`        | 8080      | **WP1** (commented placeholder)                                                |
| `admin-api/ui`   | 3001/3000 | **WP5** (commented placeholder)                                                |

Connection strings:

- Host → Mongo: `mongodb://root:devpass@localhost:27017/?replicaSet=rs0&directConnection=true`
- Container → Mongo: `mongodb://root:devpass@mongo:27017/?replicaSet=rs0&directConnection=true`
- Issuer: `http://localhost:8180/realms/mcp` (containers fetch JWKS from `http://keycloak:8080/...`)

Keycloak realm `mcp`: realm roles `mcp-developer`, `mcp-admin`; users `dev1/dev1`
(mcp-developer) and `admin1/admin1` (mcp-admin + mcp-developer); public client `mcp-gateway`
(password + auth-code/PKCE, audience mapper → `aud=mcp-gateway`); confidential client
`ci-runner` / `ci-runner-secret` (client credentials, same audience mapper).

`make up` works with Podman (`podman compose`, which delegates to `docker-compose`) and with
Docker (`make up COMPOSE="docker compose"`).
