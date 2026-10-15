import { useCallback, useEffect, useState } from "react";
import { errorText } from "../components/ErrorView";
import { HealthBadge } from "../components/HealthBadge";
import { useApp } from "../context";
import { UpstreamForm } from "../forms/UpstreamForm";
import type { Upstream, UpstreamHealth } from "../types";
import { CollectionPage } from "./CollectionPage";

export function UpstreamsPage({
  selected,
  select,
}: {
  selected: string | null;
  select(id: string | null): void;
}) {
  const { api, notify, refreshCatalog } = useApp();
  const [health, setHealth] = useState<Record<string, UpstreamHealth>>({});
  const [busy, setBusy] = useState(false);

  const loadHealth = useCallback(async () => {
    try {
      setHealth((await api.health()).upstreams);
    } catch (err) {
      notify(`health unavailable: ${errorText(err)}`, "error");
    }
  }, [api, notify]);

  useEffect(() => {
    void loadHealth();
    const t = setInterval(() => void loadHealth(), 15_000);
    return () => clearInterval(t);
  }, [loadHealth]);

  const reload = async () => {
    setBusy(true);
    try {
      setHealth((await api.reload()).upstreams);
      await refreshCatalog();
      notify("gateway reloaded its upstreams");
    } catch (err) {
      notify(`reload failed: ${errorText(err)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <CollectionPage<Upstream>
      collection="upstreams"
      title="Upstreams"
      selected={selected}
      select={select}
      toolbar={
        <button
          type="button"
          onClick={() => void reload()}
          disabled={busy}
          title="POST /api/upstreams/reload"
        >
          {busy ? "Reloading…" : "Reload gateway"}
        </button>
      }
      columns={[
        { header: "Name", cell: (u) => u.displayName },
        { header: "Endpoint", cell: (u) => <code className="small">{u.url ?? u.command}</code> },
        { header: "Health", cell: (u) => <HealthBadge health={health[u._id]} /> },
      ]}
      editor={({ key, ...props }) => <UpstreamForm key={key} {...props} />}
    />
  );
}
