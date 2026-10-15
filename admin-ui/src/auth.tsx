/**
 * Keycloak login: authorization code + PKCE (S256) with oidc-client-ts against the public
 * client `mcp-admin-ui`. Tokens live in sessionStorage (cleared with the tab); renewal
 * uses the refresh token, so no iframe is needed.
 */
import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";
import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from "react";
import type { RuntimeConfig } from "./types";

export interface AuthState {
  user: User | null;
  ready: boolean;
  error: string | null;
  login(): Promise<void>;
  logout(): Promise<void>;
  accessToken(): Promise<string | null>;
}

const AuthContext = createContext<AuthState | null>(null);

// The code can be redeemed once; StrictMode runs effects twice, so share one promise.
let callback: { search: string; done: Promise<User> } | null = null;
function redeemOnce(manager: UserManager): Promise<User> {
  const search = window.location.search;
  if (callback?.search !== search) callback = { search, done: manager.signinRedirectCallback() };
  return callback.done;
}

export function createUserManager(cfg: RuntimeConfig): UserManager {
  const origin = window.location.origin;
  return new UserManager({
    authority: cfg.oidcAuthority,
    client_id: cfg.oidcClientId,
    redirect_uri: `${origin}/`,
    post_logout_redirect_uri: `${origin}/`,
    response_type: "code",
    scope: cfg.oidcScope,
    automaticSilentRenew: true,
    monitorSession: false,
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
}

export function AuthProvider({ manager, children }: { manager: UserManager; children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const params = new URLSearchParams(window.location.search);
        if (params.has("code") && params.has("state")) {
          const u = await redeemOnce(manager);
          const hash = typeof u.state === "string" ? u.state : window.location.hash;
          window.history.replaceState({}, document.title, `${window.location.pathname}${hash}`);
          if (!cancelled) setUser(u);
        } else if (params.has("error")) {
          throw new Error(`${params.get("error")}: ${params.get("error_description") ?? ""}`);
        } else {
          const u = await manager.getUser();
          if (!cancelled) setUser(u && !u.expired ? u : null);
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    const onLoaded = (u: User) => setUser(u);
    const onUnloaded = () => setUser(null);
    manager.events.addUserLoaded(onLoaded);
    manager.events.addUserUnloaded(onUnloaded);
    manager.events.addSilentRenewError(() => setUser(null));
    return () => {
      cancelled = true;
      manager.events.removeUserLoaded(onLoaded);
      manager.events.removeUserUnloaded(onUnloaded);
    };
  }, [manager]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      ready,
      error,
      login: () => manager.signinRedirect({ state: window.location.hash }),
      logout: async () => {
        const u = await manager.getUser();
        await manager.removeUser();
        await manager.signoutRedirect({ id_token_hint: u?.id_token });
      },
      accessToken: async () => {
        const u = await manager.getUser();
        if (!u) return null;
        if (u.expired) {
          try {
            return (await manager.signinSilent())?.access_token ?? null;
          } catch {
            return null;
          }
        }
        return u.access_token;
      },
    }),
    [manager, user, ready, error],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

/** Claims of the access token (display only; the API verifies it). */
export function accessClaims(user: User | null): Record<string, unknown> {
  if (!user) return {};
  try {
    const part = user.access_token.split(".")[1] ?? "";
    return JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/"))) as Record<string, unknown>;
  } catch {
    return {};
  }
}
