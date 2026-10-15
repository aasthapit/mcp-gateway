#!/usr/bin/env bash
# `make loadtest`: start two gateways on the host against the local stack, run k6, stop them.
#   warm  :18080  production config (Mongo policy + Mongo upstreams, seeded cacheTtlSec=60)
#   cold  :18081  Mongo policy, the same two upstreams mounted statically with cache_ttl_sec=0
#   coldn :18082  as cold, but per_request upstream sessions with protocol auto (negotiates)
# Rate limits are raised (every k6 VU is the same anonymous client IP); everything else is
# the default configuration, including INFO audit logs (written to results/gateway-*.log).
# Needs: make up && make seed, k6 (brew install k6) or a container runtime (grafana/k6).
# Env: LOADTEST_DURATION (60s), LOADTEST_SCALE (1), LISTEN_VUS (50), LOADTEST_DEV1_TOKEN.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="$HERE/results"
mkdir -p "$OUT"
rm -f "$OUT/listen-streams.txt" "$OUT/k6.log" "$OUT/cpu.txt"
: "${LOADTEST_DEV1_TOKEN:?set LOADTEST_DEV1_TOKEN (make loadtest does: make token KC_USER=dev1)}"

WARM_PORT="${WARM_PORT:-18080}"
COLD_PORT="${COLD_PORT:-18081}"
COLDN_PORT="${COLDN_PORT:-18082}"
pids=()
cleanup() {
  for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
  for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
}
trap cleanup EXIT

start_gateway() { # name port extra-env...
  local name="$1" port="$2"
  shift 2
  (
    cd "$ROOT/gateway"
    exec env GATEWAY_PORT="$port" GATEWAY_METRICS_PORT=0 GATEWAY_HOST=127.0.0.1 \
      GATEWAY_LOG_LEVEL=INFO GATEWAY_LOG_JSON=true \
      GATEWAY_RATE_LIMIT_ANONYMOUS_RPS=100000 GATEWAY_RATE_LIMIT_ANONYMOUS_BURST=100000 \
      GATEWAY_RATE_LIMIT_USER_RPS=100000 GATEWAY_RATE_LIMIT_USER_BURST=100000 \
      GATEWAY_UPSTREAM_PROBE_SEC=30 "$@" \
      uv run --frozen mcp-gateway >"$OUT/gateway-$name.log" 2>&1
  ) &
  pids+=("$!")
}

start_gateway warm "$WARM_PORT"
start_gateway cold "$COLD_PORT" GATEWAY_UPSTREAMS_SOURCE=static \
  GATEWAY_STATIC_UPSTREAMS='[{"id":"sample-public","url":"http://localhost:8081/mcp","cache_ttl_sec":0,"protocol_mode":"2026-07-28","session_mode":"shared"},{"id":"sample-private","url":"http://localhost:8082/mcp","cache_ttl_sec":0,"protocol_mode":"2026-07-28","session_mode":"shared"}]'

start_gateway coldn "$COLDN_PORT" GATEWAY_UPSTREAMS_SOURCE=static \
  GATEWAY_STATIC_UPSTREAMS='[{"id":"sample-public","url":"http://localhost:8081/mcp","cache_ttl_sec":0},{"id":"sample-private","url":"http://localhost:8082/mcp","cache_ttl_sec":0}]'

for port in "$WARM_PORT" "$COLD_PORT" "$COLDN_PORT"; do
  for _ in $(seq 1 120); do
    curl -fs -o /dev/null "http://127.0.0.1:$port/readyz" && break
    sleep 0.25
  done
  curl -fsS -o /dev/null "http://127.0.0.1:$port/readyz" || {
    echo "gateway on :$port not ready (make up && make seed?); see $OUT/gateway-*.log"
    exit 1
  }
done
echo "gateways ready: warm :$WARM_PORT, cold :$COLD_PORT, cold+negotiate :$COLDN_PORT"

# Warm-up: fill the warm gateway's upstream list cache and the policy memo.
for _ in 1 2 3; do
  curl -fs -o /dev/null -X POST "http://127.0.0.1:$WARM_PORT/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}' || true
done

# Sample the listen-stream gauge and the warm gateway's CPU while k6 runs.
warm_pid=$(lsof -ti "tcp:$WARM_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
(
  for _ in $(seq 1 60); do
    sleep 5
    curl -fs "http://127.0.0.1:$WARM_PORT/metrics" | awk '/^gateway_open_listen_streams /{print $2}' >>"$OUT/listen-streams.txt" || true
    [ -n "$warm_pid" ] && ps -o %cpu= -p "$warm_pid" >>"$OUT/cpu.txt" 2>/dev/null || true
  done
) &
sampler=$!
disown "$sampler"

export GATEWAY_URL="http://127.0.0.1:$WARM_PORT" GATEWAY_COLD_URL="http://127.0.0.1:$COLD_PORT" \
  GATEWAY_COLD_NEGOTIATE_URL="http://127.0.0.1:$COLDN_PORT"
status=0
if command -v k6 >/dev/null 2>&1; then
  (cd "$HERE" && LOADTEST_OUT="$OUT" k6 run --quiet --log-output="file=$OUT/k6.log" main.js) || status=$?
else
  CONTAINER="${CONTAINER:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}"
  echo "k6 not found; running grafana/k6 in $CONTAINER (latency includes the VM network hop)"
  GATEWAY_URL="http://host.containers.internal:$WARM_PORT" GATEWAY_COLD_URL="http://host.containers.internal:$COLD_PORT"
  GATEWAY_COLD_NEGOTIATE_URL="http://host.containers.internal:$COLDN_PORT"
  "$CONTAINER" run --rm -v "$HERE:/loadtest" -w /loadtest \
    -e GATEWAY_URL -e GATEWAY_COLD_URL -e GATEWAY_COLD_NEGOTIATE_URL -e LOADTEST_DEV1_TOKEN -e LOADTEST_DURATION -e LOADTEST_SCALE -e LISTEN_VUS \
    -e LOADTEST_OUT=/loadtest/results docker.io/grafana/k6:2.3.0 run --quiet --log-output=file=/loadtest/results/k6.log main.js || status=$?
fi
kill "$sampler" 2>/dev/null || true

max_listen=$(sort -n "$OUT/listen-streams.txt" 2>/dev/null | tail -1 || true)
cpu=$(awk '{s+=$1; if($1>m)m=$1; n++} END{if(n) printf "avg %.0f %%, peak %.0f %% of one core", s/n, m}' "$OUT/cpu.txt" 2>/dev/null || true)
echo "warm gateway CPU (5 s samples): ${cpu:-n/a}"
echo "peak gateway_open_listen_streams (5 s samples): ${max_listen:-n/a}"
five=$(curl -fs "http://127.0.0.1:$WARM_PORT/metrics" | awk '/^gateway_http_responses_total\{code="5xx",route="mcp"\}/{print $2}')
echo "warm gateway HTTP 5xx on /mcp: ${five:-0}"
echo "k6 log (listen-stream client timeouts are expected): $OUT/k6.log; results: $OUT/summary.{md,json}"
{
  echo
  echo "Peak open listen streams: ${max_listen:-n/a}; warm gateway HTTP 5xx on /mcp: ${five:-0}; warm gateway CPU: ${cpu:-n/a}."
} >>"$OUT/summary.md"
exit "$status"
