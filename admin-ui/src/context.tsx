import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { Api } from "./api";
import type { CatalogTool, WriteResult } from "./types";

interface Toast {
  id: number;
  text: string;
  kind: "ok" | "error";
}

interface AppState {
  api: Api;
  version: number | null;
  /** Record a write: updates the header's meta.version and shows a toast. */
  saved(what: string, result: WriteResult<unknown>): void;
  notify(text: string, kind?: Toast["kind"]): void;
  catalog: CatalogTool[];
  catalogError: string | null;
  refreshCatalog(): Promise<void>;
}

const Ctx = createContext<AppState | null>(null);
let toastSeq = 0;

export function AppProvider({ api, children }: { api: Api; children: ReactNode }) {
  const [version, setVersion] = useState<number | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [catalog, setCatalog] = useState<CatalogTool[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const notify = useCallback((text: string, kind: Toast["kind"] = "ok") => {
    const id = ++toastSeq;
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);

  const refreshCatalog = useCallback(async () => {
    try {
      setCatalog((await api.catalog()).tools);
      setCatalogError(null);
    } catch (err) {
      setCatalogError((err as Error).message);
    }
  }, [api]);

  useEffect(() => {
    void refreshCatalog();
    const poll = () =>
      api
        .meta()
        .then((m) => setVersion(m.version))
        .catch(() => {});
    poll();
    const t = setInterval(poll, 15_000);
    return () => clearInterval(t);
  }, [api, refreshCatalog]);

  const saved = useCallback(
    (what: string, r: WriteResult<unknown>) => {
      setVersion(r.version);
      notify(
        r.op === "noop"
          ? `${what}: no changes (meta.version ${r.version})`
          : `${what} ${r.op === "delete" ? "deleted" : "saved"} — meta.version ${r.version}`,
      );
    },
    [notify],
  );

  const value = useMemo(
    () => ({ api, version, saved, notify, catalog, catalogError, refreshCatalog }),
    [api, version, saved, notify, catalog, catalogError, refreshCatalog],
  );
  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp outside AppProvider");
  return ctx;
}

/** `#/<page>/<id>` routing; ids are URL-encoded. */
export function useHashRoute(): [
  string,
  string | null,
  (page: string, id?: string | null) => void,
] {
  const parse = () => {
    const [, page = "toolsets", id] = window.location.hash.replace(/^#/, "").split("/");
    return [page || "toolsets", id ? decodeURIComponent(id) : null] as const;
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = useCallback((page: string, id?: string | null) => {
    window.location.hash = id ? `/${page}/${encodeURIComponent(id)}` : `/${page}`;
  }, []);
  return [route[0], route[1], go];
}
