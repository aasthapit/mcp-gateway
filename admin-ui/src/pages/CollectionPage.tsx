/** List on the left, editor on the right; shared by Upstreams, Toolsets and Grants. */
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { ErrorView } from "../components/ErrorView";
import { useApp } from "../context";
import type { Collection, PolicyDoc, WriteResult } from "../types";

export interface EditorProps<T extends PolicyDoc> {
  initial: T | null;
  onSave(doc: T): Promise<WriteResult<T>>;
  onDelete?(): Promise<WriteResult<T>>;
}

interface Props<T extends PolicyDoc> {
  collection: Collection;
  title: string;
  selected: string | null;
  select(id: string | null): void;
  columns: { header: string; cell(doc: T): ReactNode }[];
  editor(props: EditorProps<T> & { key: string }): ReactNode;
  toolbar?: ReactNode;
}

export function CollectionPage<T extends PolicyDoc>(p: Props<T>) {
  const { api, saved } = useApp();
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(async () => {
    try {
      const r = await api.list<T>(p.collection);
      setItems(r.items);
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [api, p.collection]);

  useEffect(() => {
    void load();
  }, [load]);

  const current =
    p.selected && p.selected !== "new" ? (items?.find((d) => d._id === p.selected) ?? null) : null;
  const editing = p.selected === "new" || current !== null;

  const onSave = async (doc: T) => {
    const r = await api.put<T>(p.collection, doc);
    saved(`${p.collection}/${doc._id}`, r);
    await load();
    if (p.selected === "new") p.select(doc._id);
    return r;
  };
  const onDelete = current
    ? async () => {
        const r = await api.remove<T>(p.collection, current._id);
        saved(`${p.collection}/${current._id}`, r);
        await load();
        p.select(null);
        return r;
      }
    : undefined;

  return (
    <div className="split">
      <section className="list">
        <header>
          <h1>{p.title}</h1>
          <div className="toolbar">
            {p.toolbar}
            <button type="button" className="primary" onClick={() => p.select("new")}>
              New
            </button>
          </div>
        </header>
        <ErrorView error={error} />
        {items === null && !error && <p className="muted">loading…</p>}
        {items && items.length === 0 && <p className="muted">none yet</p>}
        {items && items.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Id</th>
                {p.columns.map((c) => (
                  <th key={c.header}>{c.header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr
                  key={d._id}
                  className={d._id === p.selected ? "selected" : ""}
                  onClick={() => p.select(d._id)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && p.select(d._id)}
                >
                  <td>
                    <code>{d._id}</code>
                    {"enabled" in d && !d.enabled && (
                      <span className="badge badge-down">disabled</span>
                    )}
                  </td>
                  {p.columns.map((c) => (
                    <td key={c.header}>{c.cell(d)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="detail">
        {editing ? (
          p.editor({ key: current?._id ?? "new", initial: current, onSave, onDelete })
        ) : (
          <p className="muted">Select an entry or create a new one.</p>
        )}
        {current?.updatedAt && (
          <p className="muted stamp">
            last updated {new Date(current.updatedAt).toLocaleString()} by{" "}
            <code>{current.updatedBy}</code>
          </p>
        )}
      </section>
    </div>
  );
}
