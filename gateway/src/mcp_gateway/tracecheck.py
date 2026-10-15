"""``make traces``: one traced ``tools/call`` through an in-process gateway, then its spans.

Starts the gateway on a free port against the local stack (Mongo policy, sample upstreams)
with ``GATEWAY_TELEMETRY_MODE=native`` exporting to the local collector
(``GATEWAY_OTLP_ENDPOINT``, default ``http://localhost:4318``), sends one guest
``tools/call sample-public_echo`` carrying a fresh W3C ``traceparent``, flushes the spans
and then reads the collector's debug-exporter log (``TRACES_LOGS_CMD``, default
``podman logs mcp-otel-collector``) until the spans of that trace show up. Prints them and
exits non-zero when the gateway's SERVER span with the gateway attributes is missing.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time

import httpx
import uvicorn

TOOL = "sample-public_echo"
EXPECTED_ATTRS = ("gateway.principal.kind", "gateway.policy.version", "gen_ai.tool.name")


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spans_for(trace_id: str, log: str) -> list[list[str]]:
    """Debug-exporter span blocks (``Span #n`` ... next block) of one trace."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in log.splitlines():
        if re.match(r"^\s*Span #\d+", line) or line.startswith(("ResourceSpans #", "ScopeSpans #")):
            if current is not None:
                blocks.append(current)
            current = [] if "Span #" in line and not line.startswith("ScopeSpans") else None
            continue
        if current is not None:
            current.append(line.rstrip())
    if current is not None:
        blocks.append(current)
    return [b for b in blocks if any(f"Trace ID       : {trace_id}" in line for line in b)]


def main() -> int:
    endpoint = os.environ.get("GATEWAY_OTLP_ENDPOINT", "http://localhost:4318")
    os.environ.update(
        {
            "GATEWAY_TELEMETRY_MODE": "native",
            "GATEWAY_OTLP_ENDPOINT": endpoint,
            "GATEWAY_METRICS_PORT": "0",
            "GATEWAY_LOG_LEVEL": os.environ.get("GATEWAY_LOG_LEVEL", "WARNING"),
            "GATEWAY_LOG_JSON": "false",
        }
    )
    from mcp_gateway.app import build_gateway
    from mcp_gateway.settings import load_settings
    from mcp_gateway.telemetry import shutdown_tracing

    settings = load_settings()
    gw = build_gateway(settings)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(gw.app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(base + "/readyz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        print("FAIL gateway did not become ready (make up && make seed?)")
        return 1

    trace_id = secrets.token_hex(16)
    traceparent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    r = httpx.post(
        base + "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": TOOL,
                "arguments": {"text": "traced"},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": TOOL,
            "traceparent": traceparent,
        },
        timeout=15,
    )
    ok_call = r.status_code == 200 and "result" in r.json()
    print(f"  {'ok' if ok_call else 'FAIL'}  guest tools/call {TOOL} -> HTTP {r.status_code}")
    print(f"        traceparent {traceparent}  (OTLP {endpoint})")
    shutdown_tracing()  # flush the batch processor
    server.should_exit = True
    thread.join(15)

    logs_cmd = shlex.split(os.environ.get("TRACES_LOGS_CMD", "podman logs mcp-otel-collector"))
    spans: list[list[str]] = []
    for _ in range(20):
        out = subprocess.run(logs_cmd, capture_output=True, text=True, check=False)
        spans = _spans_for(trace_id, out.stdout + out.stderr)
        if any("Kind           : Server" in line for b in spans for line in b):
            break
        time.sleep(0.5)
    if not spans:
        print(f"FAIL no spans for trace {trace_id} in `{' '.join(logs_cmd)}`")
        return 1
    print(f"  ok  collector received {len(spans)} span(s) for trace {trace_id}:")
    server_ok = False
    for block in spans:
        fields = {
            k.strip(): v.strip()
            for k, _, v in (line.partition(":") for line in block if " : " in line)
        }
        attrs = [line.strip()[3:] for line in block if line.strip().startswith("-> ")]
        print(f"      - {fields.get('Name')}  [{fields.get('Kind')}]")
        for a in attrs:
            print(f"          {a}")
        if fields.get("Kind") == "Server" and all(
            any(a.startswith(name + ":") for a in attrs) for name in EXPECTED_ATTRS
        ):
            server_ok = True
    print(f"  {'ok' if server_ok else 'FAIL'}  SERVER span carries " + ", ".join(EXPECTED_ATTRS))
    return 0 if (ok_call and server_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
