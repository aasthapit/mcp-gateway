#!/usr/bin/env bash
# `make admin-dev` — admin API (tsx watch) + admin UI (Vite) on the host with hot reload,
# against the compose stack (`make up && make seed`). Also starts the gateway on
# GATEWAY_PORT unless one already answers there, so the catalog, preview and a real MCP
# client work end to end. Ctrl-C stops everything this script started.
#
#   UI     http://localhost:${ADMIN_UI_PORT:-3000}   (log in as admin1 / admin1)
#   API    http://localhost:${ADMIN_API_PORT:-3001}
#   MCP    http://localhost:${GATEWAY_PORT:-8080}/mcp
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
# shellcheck disable=SC1091
[ -f "$HERE/.env" ] && set -a && . "$HERE/.env" && set +a

: "${GATEWAY_PORT:=8080}" "${ADMIN_API_PORT:=3001}" "${ADMIN_UI_PORT:=3000}" "${KEYCLOAK_PORT:=8180}"
: "${GATEWAY_INTERNAL_TOKEN:=dev-internal-token-change-me}"
export ADMIN_PORT="$ADMIN_API_PORT"
export ADMIN_GATEWAY_INTERNAL_URL="${ADMIN_GATEWAY_INTERNAL_URL:-http://127.0.0.1:$GATEWAY_PORT}"
export ADMIN_GATEWAY_INTERNAL_TOKEN="${ADMIN_GATEWAY_INTERNAL_TOKEN:-$GATEWAY_INTERNAL_TOKEN}"
export ADMIN_UI_PORT ADMIN_UI_GATEWAY_MCP_URL="${ADMIN_UI_GATEWAY_MCP_URL:-http://localhost:$GATEWAY_PORT/mcp}"

for pkg in admin-api admin-ui; do
  [ -d "$ROOT/$pkg/node_modules" ] || (cd "$ROOT/$pkg" && npm ci --no-audit --no-fund)
done
for port in "$ADMIN_API_PORT" "$ADMIN_UI_PORT"; do
  if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$port/"; then
    echo "port $port is already in use (make admin-down if the admin containers run)"; exit 1
  fi
done
curl -fs -o /dev/null "http://localhost:$KEYCLOAK_PORT/realms/mcp/.well-known/openid-configuration" ||
  { echo "keycloak not reachable on :$KEYCLOAK_PORT: run make up (and make seed)"; exit 1; }

# Job control: every background pipeline gets its own process group, so cleanup can stop
# npm/tsx/vite/uvicorn and their children, not just the log prefixer.
set -m
cleanup() {
  trap - INT TERM EXIT
  for pgid in $(jobs -p); do kill -TERM -- "-$pgid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

prefix() { awk -v p="[$1] " '{ print p $0; fflush() }'; }

if curl -fs -o /dev/null --max-time 1 "http://127.0.0.1:$GATEWAY_PORT/healthz"; then
  echo "[admin-dev] reusing the gateway already on :$GATEWAY_PORT (it must run with GATEWAY_INTERNAL_TOKEN from local/.env)"
else
  (cd "$ROOT/gateway" && GATEWAY_PORT="$GATEWAY_PORT" GATEWAY_INTERNAL_TOKEN="$GATEWAY_INTERNAL_TOKEN" \
    GATEWAY_PUBLIC_BASE_URL="http://localhost:$GATEWAY_PORT" exec uv run --frozen mcp-gateway) 2>&1 | prefix gateway &
fi
(cd "$ROOT/admin-api" && exec npm run --silent dev) 2>&1 | prefix api &
(cd "$ROOT/admin-ui" && exec npm run --silent dev -- --clearScreen false) 2>&1 | prefix ui &

for i in $(seq 1 60); do
  curl -fs -o /dev/null "http://127.0.0.1:$ADMIN_API_PORT/readyz" && curl -fs -o /dev/null "http://127.0.0.1:$ADMIN_UI_PORT/config.json" && break
  sleep 1
done
cat <<EOF
[admin-dev] ready:
  UI   http://localhost:$ADMIN_UI_PORT     (Keycloak login: admin1 / admin1)
  API  http://localhost:$ADMIN_API_PORT     (bearer token: make token-admin)
  MCP  http://localhost:$GATEWAY_PORT/mcp (guest without a token)
  Ctrl-C stops everything.
EOF
wait
