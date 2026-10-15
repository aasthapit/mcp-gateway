// Mirrors ../schemas/*.schema.json (the API validates every write against those files).

export type Collection = "upstreams" | "toolsets" | "grants";

export interface Stamped {
  updatedAt?: string;
  updatedBy?: string;
}

export interface Upstream extends Stamped {
  _id: string;
  displayName: string;
  enabled: boolean;
  transport: "http" | "sse" | "stdio";
  url?: string;
  command?: string;
  args?: string[];
  auth: {
    type: "none" | "static_bearer" | "static_header" | "client_credentials";
    secretRef?: string;
    headerName?: string;
  };
  sessionMode: "shared" | "per_request";
  protocolMode?: "auto" | "2026-07-28" | "legacy";
  timeoutsMs?: { connect?: number; call?: number };
  cacheTtlSec?: number;
  tags?: string[];
}

export interface Toolset extends Stamped {
  _id: string;
  description?: string;
  enabled: boolean;
  allow: string[];
  deny?: string[];
}

export type ClaimOp = "eq" | "in" | "contains" | "exists";
export type Scalar = string | number | boolean;
export type Subject =
  | { kind: "anonymous" }
  | { kind: "authenticated" }
  | { kind: "claim"; path: string; op: ClaimOp; value?: Scalar | Scalar[] }
  | { kind: "client_id"; value: string };

export interface Grant extends Stamped {
  _id: string;
  description?: string;
  enabled: boolean;
  priority?: number;
  subject: Subject;
  toolsets: string[];
}

export type PolicyDoc = Upstream | Toolset | Grant;

export interface CatalogTool {
  namespaced_name: string;
  upstream: string;
  name: string;
  description?: string | null;
}

export interface UpstreamHealth {
  status: "up" | "down" | "degraded" | "unknown";
  reason?: string | null;
  error?: string | null;
  latency_ms?: number | null;
  tool_count?: number | null;
  checked_at?: string | null;
}

export interface WriteResult<T> {
  op: "create" | "update" | "delete" | "noop";
  version: number;
  document: T | null;
  warnings?: string[];
}

export interface HistoryEntry {
  _id: string;
  collection: Collection;
  id: string;
  op: "create" | "update" | "delete";
  actor: string;
  actorName?: string | null;
  at: string;
  version: number;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface PreviewResult {
  input: { mode: "anonymous" | "claims" | "token"; verified: boolean; note?: string };
  claims?: Record<string, unknown>;
  principal?: Record<string, unknown>;
  policy_version?: number;
  tools: string[];
  grants: string[] | null;
}

export interface RuntimeConfig {
  apiBaseUrl: string;
  oidcAuthority: string;
  oidcClientId: string;
  oidcScope: string;
  gatewayMcpUrl: string;
  environment: string;
}
