// Runtime configuration served as /config.json, by the Vite dev server (vite.config.ts) and
// by server.mjs in the container, from ADMIN_UI_* environment variables. One image for every
// environment: nothing is baked in at build time.
export function runtimeConfig(env = process.env) {
  return {
    // "" = same origin (OpenShift: the Route sends /api to the admin-api Service).
    apiBaseUrl: env.ADMIN_UI_API_BASE_URL ?? "http://localhost:3001",
    oidcAuthority: env.ADMIN_UI_OIDC_AUTHORITY ?? "http://localhost:8180/realms/mcp",
    oidcClientId: env.ADMIN_UI_OIDC_CLIENT_ID ?? "mcp-admin-ui",
    oidcScope: env.ADMIN_UI_OIDC_SCOPE ?? "openid profile",
    // Shown on the Preview page so admins can point a real MCP client at the gateway.
    gatewayMcpUrl: env.ADMIN_UI_GATEWAY_MCP_URL ?? "http://localhost:8080/mcp",
    environment: env.ADMIN_UI_ENVIRONMENT ?? "local",
  };
}
