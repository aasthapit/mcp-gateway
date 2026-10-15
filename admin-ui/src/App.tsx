import { useMemo } from "react";
import { Api } from "./api";
import { accessClaims, useAuth } from "./auth";
import { AppProvider, useApp, useHashRoute } from "./context";
import { GrantsPage } from "./pages/Grants";
import { HistoryPage } from "./pages/History";
import { PreviewPage } from "./pages/Preview";
import { ToolsetsPage } from "./pages/Toolsets";
import { UpstreamsPage } from "./pages/Upstreams";
import type { RuntimeConfig } from "./types";

const PAGES = [
  ["upstreams", "Upstreams"],
  ["toolsets", "Toolsets"],
  ["grants", "Grants"],
  ["preview", "Preview"],
  ["history", "History"],
] as const;

export function App({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const api = useMemo(
    () => new Api(config.apiBaseUrl, auth.accessToken),
    [config.apiBaseUrl, auth.accessToken],
  );

  if (!auth.ready) return <div className="center muted">loading…</div>;
  if (!auth.user) {
    return (
      <div className="center login">
        <h1>MCP Gateway Admin</h1>
        <p className="muted">Sign in with an account that has the realm role mcp-admin.</p>
        {auth.error && <div className="error">{auth.error}</div>}
        <button className="primary" onClick={() => void auth.login()}>
          Sign in with Keycloak
        </button>
        <p className="muted small">{config.oidcAuthority}</p>
      </div>
    );
  }
  return (
    <AppProvider api={api}>
      <Shell config={config} />
    </AppProvider>
  );
}

function Shell({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const { version } = useApp();
  const [page, id, go] = useHashRoute();
  const claims = accessClaims(auth.user);
  const name =
    (claims.preferred_username as string | undefined) ?? (claims.sub as string | undefined);
  const select = (next: string | null) => go(page, next);

  return (
    <div className="shell">
      <nav>
        <span className="brand">MCP Gateway Admin</span>
        {PAGES.map(([key, label]) => (
          <a key={key} href={`#/${key}`} className={page === key ? "active" : ""}>
            {label}
          </a>
        ))}
        <span className="spacer" />
        <span className="meta" title="meta.version: bumped by every policy write">
          policy v<strong data-testid="header-version">{version ?? "…"}</strong>
        </span>
        <span className="env">{config.environment}</span>
        <span className="user">{name}</span>
        <button className="link" onClick={() => void auth.logout()}>
          Sign out
        </button>
      </nav>
      <main>
        {page === "upstreams" && <UpstreamsPage selected={id} select={select} />}
        {page === "toolsets" && <ToolsetsPage selected={id} select={select} />}
        {page === "grants" && <GrantsPage selected={id} select={select} />}
        {page === "preview" && <PreviewPage config={config} />}
        {page === "history" && <HistoryPage />}
      </main>
    </div>
  );
}
