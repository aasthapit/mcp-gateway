import { Fragment, useCallback, useEffect, useState } from "react";
import { ErrorView } from "../components/ErrorView";
import { JsonDiff } from "../components/JsonDiff";
import { useApp } from "../context";
import type { HistoryEntry } from "../types";

export function HistoryPage() {
  const { api } = useApp();
  const [collection, setCollection] = useState("");
  const [id, setId] = useState("");
  const [limit, setLimit] = useState(100);
  const [items, setItems] = useState<HistoryEntry[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setItems((await api.history({ collection, id: id.trim(), limit })).items);
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [api, collection, id, limit]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page">
      <h1>History</h1>
      <div className="row filters">
        <label>
          Collection
          <select value={collection} onChange={(e) => setCollection(e.target.value)}>
            <option value="">all</option>
            <option>upstreams</option>
            <option>toolsets</option>
            <option>grants</option>
          </select>
        </label>
        <label>
          Id
          <input value={id} onChange={(e) => setId(e.target.value)} placeholder="any" />
        </label>
        <label>
          Limit
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[25, 100, 500].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      <ErrorView error={error} />
      {items && (
        <table className="history">
          <thead>
            <tr>
              <th>When</th>
              <th>Version</th>
              <th>Change</th>
              <th>Actor</th>
            </tr>
          </thead>
          <tbody>
            {items.map((h) => (
              <Fragment key={h._id}>
                <tr
                  className={open === h._id ? "selected" : ""}
                  onClick={() => setOpen(open === h._id ? null : h._id)}
                  tabIndex={0}
                >
                  <td>{new Date(h.at).toLocaleString()}</td>
                  <td>{h.version}</td>
                  <td>
                    <span className={`badge badge-op-${h.op}`}>{h.op}</span> {h.collection}/
                    <code>{h.id}</code>
                  </td>
                  <td title={h.actor}>{h.actorName ?? h.actor}</td>
                </tr>
                {open === h._id && (
                  <tr className="diff-row">
                    <td colSpan={4}>
                      <JsonDiff before={h.before} after={h.after} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
      {items?.length === 0 && <p className="muted">no history entries</p>}
    </div>
  );
}
