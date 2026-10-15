import type {
  CatalogTool,
  Collection,
  HistoryEntry,
  PolicyDoc,
  PreviewResult,
  UpstreamHealth,
  WriteResult,
} from "./types";

export interface Issue {
  path: string;
  message: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: {
      error?: string;
      details?: Issue[] | string;
      referencedBy?: Record<string, string[]>;
    } & Record<string, unknown>,
  ) {
    super(body.error ?? `HTTP ${status}`);
  }
}

export type PreviewInput =
  { kind: "anonymous" } | { claims: Record<string, unknown> } | { token: string };

export class Api {
  constructor(
    private readonly baseUrl: string,
    private readonly token: () => Promise<string | null>,
  ) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const token = await this.token();
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const json = text ? (JSON.parse(text) as unknown) : null;
    if (!res.ok) throw new ApiError(res.status, (json as ApiError["body"]) ?? {});
    return json as T;
  }

  meta = () =>
    this.request<{ version: number; updatedAt?: string; updatedBy?: string }>("GET", "/api/meta");
  list = <T extends PolicyDoc>(c: Collection) =>
    this.request<{ items: T[]; version: number }>("GET", `/api/${c}`);
  get = <T extends PolicyDoc>(c: Collection, id: string) =>
    this.request<T>("GET", `/api/${c}/${encodeURIComponent(id)}`);
  put = <T extends PolicyDoc>(c: Collection, doc: T) =>
    this.request<WriteResult<T>>(
      "PUT",
      `/api/${c}/${encodeURIComponent(doc._id)}`,
      stripStamps(doc),
    );
  remove = <T extends PolicyDoc>(c: Collection, id: string) =>
    this.request<WriteResult<T>>("DELETE", `/api/${c}/${encodeURIComponent(id)}`);
  history = (q: { collection?: string; id?: string; limit?: number }) => {
    const params = new URLSearchParams();
    if (q.collection) params.set("collection", q.collection);
    if (q.id) params.set("id", q.id);
    params.set("limit", String(q.limit ?? 100));
    return this.request<{ items: HistoryEntry[] }>("GET", `/api/history?${params}`);
  };
  catalog = () =>
    this.request<{ upstreams: string[]; tools: CatalogTool[] }>("GET", "/api/catalog");
  health = () =>
    this.request<{ upstreams: Record<string, UpstreamHealth>; probe_interval_sec?: number }>(
      "GET",
      "/api/upstreams/health",
    );
  reload = () =>
    this.request<{ upstreams: Record<string, UpstreamHealth> }>("POST", "/api/upstreams/reload");
  preview = (input: PreviewInput) => this.request<PreviewResult>("POST", "/api/preview", input);
}

export function stripStamps<T extends object>(doc: T): T {
  const {
    updatedAt: _a,
    updatedBy: _b,
    ...rest
  } = doc as T & { updatedAt?: unknown; updatedBy?: unknown };
  return rest as T;
}
