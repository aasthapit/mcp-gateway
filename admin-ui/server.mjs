// Production server for the built UI (container CMD): static files from dist/, the runtime
// /config.json from ADMIN_UI_* env, /healthz, SPA fallback and security headers. Node
// built-ins only, so the runtime image needs no node_modules.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runtimeConfig } from "./runtime-config.mjs";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "dist");
const port = Number(process.env.ADMIN_UI_PORT ?? process.env.PORT ?? 3000);
const host = process.env.ADMIN_UI_HOST ?? "0.0.0.0";
const config = runtimeConfig();
const config_json = JSON.stringify(config);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".map": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

const origin = (url) => {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
};
// The SPA talks to the API and to the IdP (token endpoint, discovery): nothing else.
const connect = ["'self'", origin(config.apiBaseUrl), origin(config.oidcAuthority)].filter(Boolean);
const HEADERS = {
  "content-security-policy": [
    "default-src 'self'",
    `connect-src ${[...new Set(connect)].join(" ")}`,
    "img-src 'self' data:",
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    `form-action 'self' ${origin(config.oidcAuthority)}`,
  ].join("; "),
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
  "cross-origin-opener-policy": "same-origin",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
};

function log(req, status) {
  if (req.url === "/healthz") return;
  process.stdout.write(
    `${JSON.stringify({ time: new Date().toISOString(), method: req.method, path: req.url?.split("?")[0], status })}\n`,
  );
}

async function file(p) {
  try {
    const s = await stat(p);
    return s.isFile() ? p : null;
  } catch {
    return null;
  }
}

const server = createServer(async (req, res) => {
  for (const [k, v] of Object.entries(HEADERS)) res.setHeader(k, v);
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { allow: "GET, HEAD" }).end();
    return log(req, 405);
  }
  const url = new URL(req.url ?? "/", "http://ui");
  if (url.pathname === "/healthz") {
    res.writeHead(200, { "content-type": "application/json" }).end('{"status":"ok"}');
    return;
  }
  if (url.pathname === "/config.json") {
    res
      .writeHead(200, { "content-type": "application/json", "cache-control": "no-store" })
      .end(config_json);
    return log(req, 200);
  }
  const wanted = path.normalize(path.join(root, decodeURIComponent(url.pathname)));
  const found = wanted.startsWith(root) ? await file(wanted) : null;
  // Unknown paths get index.html (client-side routes); assets are immutable (hashed names).
  const target = found ?? path.join(root, "index.html");
  const ext = path.extname(target);
  res.writeHead(200, {
    "content-type": TYPES[ext] ?? "application/octet-stream",
    "cache-control": target.includes(`${path.sep}assets${path.sep}`)
      ? "public, max-age=31536000, immutable"
      : "no-cache",
  });
  if (req.method === "HEAD") res.end();
  else createReadStream(target).pipe(res);
  log(req, 200);
});

server.listen(port, host, () => {
  process.stdout.write(
    `${JSON.stringify({ msg: "admin-ui ready", port, apiBaseUrl: config.apiBaseUrl })}\n`,
  );
});
const stop = () => server.close(() => process.exit(0));
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
