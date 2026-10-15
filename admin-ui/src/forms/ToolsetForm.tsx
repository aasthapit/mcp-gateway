import { useMemo, useState } from "react";
import { ErrorView } from "../components/ErrorView";
import { PatternList } from "../components/PatternList";
import { SaveStatus } from "../components/SaveStatus";
import { effective } from "../glob";
import type { CatalogTool, Toolset, WriteResult } from "../types";
import { ID_RE, useSave } from "./useSave";

interface Props {
  initial: Toolset | null;
  catalog: CatalogTool[];
  onSave(doc: Toolset): Promise<WriteResult<Toolset>>;
  onDelete?(): Promise<WriteResult<Toolset>>;
}

export const emptyToolset = (): Toolset => ({
  _id: "",
  description: "",
  enabled: true,
  allow: [],
  deny: [],
});

export function ToolsetForm({ initial, catalog, onSave, onDelete }: Props) {
  const isNew = initial === null;
  const [doc, setDoc] = useState<Toolset>(() => ({ deny: [], ...(initial ?? emptyToolset()) }));
  const save = useSave<Toolset>();
  const names = useMemo(() => catalog.map((t) => t.namespaced_name).sort(), [catalog]);
  const allowed = useMemo(
    () => effective(doc.allow, doc.deny ?? [], names),
    [doc.allow, doc.deny, names],
  );
  const idOk = ID_RE.test(doc._id);

  const submit = () => {
    const out: Toolset = {
      ...doc,
      description: doc.description || undefined,
      deny: doc.deny ?? [],
    };
    if (!out.description) delete out.description;
    return save.run(() => onSave(out));
  };

  return (
    <form
      className="editor"
      aria-label="toolset"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h2>{isNew ? "New toolset" : `Toolset ${doc._id}`}</h2>
      {isNew && (
        <label>
          Id
          <input
            value={doc._id}
            placeholder="engineering"
            onChange={(e) => setDoc({ ...doc, _id: e.target.value })}
          />
          {doc._id && !idOk && (
            <span className="warning">lowercase letters, digits and -, starting with a letter</span>
          )}
        </label>
      )}
      <label>
        Description
        <input
          value={doc.description ?? ""}
          onChange={(e) => setDoc({ ...doc, description: e.target.value })}
        />
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={doc.enabled}
          onChange={(e) => setDoc({ ...doc, enabled: e.target.checked })}
        />
        Enabled
      </label>
      <PatternList
        label="Allow"
        value={doc.allow}
        catalog={catalog}
        onChange={(allow) => setDoc({ ...doc, allow })}
      />
      <PatternList
        label="Deny"
        value={doc.deny ?? []}
        catalog={catalog}
        onChange={(deny) => setDoc({ ...doc, deny })}
      />
      <div className="summary" data-testid="effective">
        Allows {allowed.length} of {names.length} catalog tools
        {allowed.length > 0 && <span className="muted">: {allowed.join(", ")}</span>}
      </div>
      <div className="actions">
        <button type="submit" className="primary" disabled={save.busy || !idOk}>
          {save.busy ? "Saving…" : "Save"}
        </button>
        {onDelete && (
          <button
            type="button"
            className="danger"
            disabled={save.busy}
            onClick={() => {
              if (window.confirm(`Delete toolset ${doc._id}?`)) void save.run(onDelete);
            }}
          >
            Delete
          </button>
        )}
      </div>
      <SaveStatus result={save.result} />
      <ErrorView error={save.error} />
    </form>
  );
}
