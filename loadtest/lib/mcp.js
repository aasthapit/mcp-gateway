// Minimal MCP-over-HTTP client for k6: one stateless modern-era (2026-07-28) JSON-RPC POST
// per call, the way a modern client talks to the gateway (no initialize, no session).
// Every request carries the protocol envelope in `_meta` plus the routing headers
// (`Mcp-Method`, `Mcp-Name`) the 2026-07-28 transport requires.
import http from "k6/http";

export const PROTOCOL = "2026-07-28";

let nextId = 1;

export function rpc(baseUrl, method, params, { token, tags, timeout } = {}) {
  const body = {
    jsonrpc: "2.0",
    id: nextId++,
    method,
    params: {
      ...(params || {}),
      _meta: {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
      },
    },
  };
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL,
    "Mcp-Method": method,
  };
  if (params && params.name) headers["Mcp-Name"] = params.name;
  if (token) headers.Authorization = `Bearer ${token}`;
  return http.post(`${baseUrl}/mcp`, JSON.stringify(body), {
    headers,
    tags,
    timeout: timeout || "30s",
  });
}

// The JSON-RPC message of a response: JSON body, or the last `data:` line of an SSE body.
export function message(res) {
  if (!res || res.status !== 200 || !res.body) return null;
  const type = res.headers["Content-Type"] || "";
  let raw = res.body;
  if (type.startsWith("text/event-stream")) {
    const data = raw
      .split("\n")
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).trim());
    raw = data[data.length - 1];
  }
  try {
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
}

export function ok(res) {
  const msg = message(res);
  return !!(msg && msg.result && !msg.result.isError);
}

export function toolNames(res) {
  const msg = message(res);
  return msg && msg.result && msg.result.tools ? msg.result.tools.map((t) => t.name) : [];
}
