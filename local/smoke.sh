#!/usr/bin/env bash
# `make smoke` — end-to-end checks of the local stack.
# WP0: infra checks only. WP1+ will extend this to run an MCP client as guest and as dev1
# through the gateway and assert the tool lists differ.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[ -f "$HERE/.env" ] && set -a && . "$HERE/.env" && set +a

: "${MONGO_ROOT_USERNAME:=root}" "${MONGO_ROOT_PASSWORD:=devpass}" "${MONGO_PORT:=27017}"
: "${KEYCLOAK_PORT:=8180}" "${SAMPLE_PUBLIC_PORT:=8081}" "${SAMPLE_PRIVATE_PORT:=8082}" "${MONGO_DB:=mcp_gateway}"
COMPOSE="${COMPOSE:-podman compose}"
KC="http://localhost:${KEYCLOAK_PORT}/realms/mcp"

pass() { printf '  \033[32mok\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

decode_claims() {  # $1 = JWT -> prints payload JSON
  python3 -c 'import sys,base64,json; p=sys.argv[1].split(".")[1]; print(json.dumps(json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4)))))' "$1"
}

mongo_eval() {  # $1 = JS; runs mongosh inside the mongo-init container (root, authSource admin)
  $COMPOSE -f "$HERE/compose.yaml" --env-file "$HERE/.env" run --rm -T mongo-init \
    mongosh --quiet "mongodb://${MONGO_ROOT_USERNAME}:${MONGO_ROOT_PASSWORD}@mongo:27017/?directConnection=true" \
    --eval "$1" 2>/dev/null | tail -1 || true
}

echo "mongo"
status=$(mongo_eval 'const s=rs.status(); print(s.set+" "+s.members[0].stateStr)')
[ "$status" = "rs0 PRIMARY" ] && pass "rs.status(): $status" || fail "rs.status(): '$status'"
version=$(mongo_eval "const m=db.getSiblingDB('${MONGO_DB}').meta.findOne({_id:'policy'}); print(m ? m.version : 'none')")
[ -n "$version" ] && [ "$version" != "none" ] && pass "meta.version = $version (seeded)" || fail "meta.version missing (got '$version') — run make seed"

echo "keycloak"
disc=$(curl -fsS "$KC/.well-known/openid-configuration") || fail "OIDC discovery at $KC"
pass "OIDC discovery issuer=$(printf '%s' "$disc" | python3 -c 'import sys,json; print(json.load(sys.stdin)["issuer"])')"
tok=$(curl -fsS -d client_id=mcp-gateway -d grant_type=password -d username=dev1 -d password=dev1 \
  "$KC/protocol/openid-connect/token" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])') || fail "password grant for dev1"
claims=$(decode_claims "$tok")
printf '%s' "$claims" | python3 -c '
import sys, json
c = json.load(sys.stdin)
aud = c.get("aud"); aud = aud if isinstance(aud, list) else [aud]
assert "mcp-gateway" in aud, f"aud={aud}"
roles = c.get("realm_access", {}).get("roles", [])
assert "mcp-developer" in roles, f"roles={roles}"
print(f"dev1 token: aud={aud} roles={roles}")' && pass "dev1 token carries aud=mcp-gateway and role mcp-developer" || fail "dev1 token claims"
tok=$(curl -fsS -d client_id=ci-runner -d client_secret=ci-runner-secret -d grant_type=client_credentials \
  "$KC/protocol/openid-connect/token" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])') || fail "client credentials for ci-runner"
decode_claims "$tok" | python3 -c '
import sys, json
c = json.load(sys.stdin)
aud = c.get("aud"); aud = aud if isinstance(aud, list) else [aud]
assert "mcp-gateway" in aud, f"aud={aud}"
azp = c.get("azp")
assert azp == "ci-runner", f"azp={azp}"' && pass "ci-runner token carries aud=mcp-gateway, azp=ci-runner" || fail "ci-runner token claims"

echo "sample upstreams"
for p in "$SAMPLE_PUBLIC_PORT" "$SAMPLE_PRIVATE_PORT"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${p}/mcp")
  [ "$code" = "405" ] || [ "$code" = "406" ] || [ "$code" = "400" ] && pass "upstream on :$p answers /mcp (HTTP $code)" || fail "upstream on :$p (HTTP $code)"
done

# The gateway checks (guest, garbage bearer, dev1 via GATEWAY_SMOKE_TOKEN) run from the Makefile:
# gateway/src/mcp_gateway/smoke.py. Tool lists differ per caller once WP3 lands the Mongo policy.
