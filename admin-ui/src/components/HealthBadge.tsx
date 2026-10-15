import type { UpstreamHealth } from "../types";

export function HealthBadge({ health }: { health: UpstreamHealth | undefined }) {
  const status = health?.status ?? "unknown";
  const title = [
    health?.reason,
    health?.error,
    health?.latency_ms != null ? `${health.latency_ms} ms` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <span className={`badge badge-${status}`} title={title || status}>
      {status}
      {health?.tool_count != null && status === "up" ? ` · ${health.tool_count} tools` : ""}
    </span>
  );
}
