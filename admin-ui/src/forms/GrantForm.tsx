import { useState } from "react";
import { ErrorView } from "../components/ErrorView";
import { SaveStatus } from "../components/SaveStatus";
import { SubjectBuilder } from "../components/SubjectBuilder";
import type { Grant, Toolset, WriteResult } from "../types";
import { ID_RE, useSave } from "./useSave";

interface Props {
  initial: Grant | null;
  toolsets: Toolset[];
  onSave(doc: Grant): Promise<WriteResult<Grant>>;
  onDelete?(): Promise<WriteResult<Grant>>;
}

export const emptyGrant = (): Grant => ({
  _id: "",
  description: "",
  enabled: true,
  priority: 100,
  subject: { kind: "anonymous" },
  toolsets: [],
});

export function GrantForm({ initial, toolsets, onSave, onDelete }: Props) {
  const isNew = initial === null;
  const [doc, setDoc] = useState<Grant>(() => initial ?? emptyGrant());
  const save = useSave<Grant>();
  const idOk = ID_RE.test(doc._id);
  const known = new Set(toolsets.map((t) => t._id));
  const unknown = doc.toolsets.filter((t) => !known.has(t));

  const toggle = (id: string, on: boolean) =>
    setDoc({ ...doc, toolsets: on ? [...doc.toolsets, id] : doc.toolsets.filter((t) => t !== id) });

  const submit = () => {
    const out: Grant = { ...doc };
    if (!out.description) delete out.description;
    if (out.priority === undefined || Number.isNaN(out.priority)) delete out.priority;
    return save.run(() => onSave(out));
  };

  return (
    <form
      className="editor"
      aria-label="grant"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h2>{isNew ? "New grant" : `Grant ${doc._id}`}</h2>
      {isNew && (
        <label>
          Id
          <input
            value={doc._id}
            placeholder="developers"
            onChange={(e) => setDoc({ ...doc, _id: e.target.value })}
          />
        </label>
      )}
      <label>
        Description
        <input
          value={doc.description ?? ""}
          onChange={(e) => setDoc({ ...doc, description: e.target.value })}
        />
      </label>
      <div className="row">
        <label className="check">
          <input
            type="checkbox"
            checked={doc.enabled}
            onChange={(e) => setDoc({ ...doc, enabled: e.target.checked })}
          />
          Enabled
        </label>
        <label>
          Priority <span className="muted">(display/audit order only)</span>
          <input
            type="number"
            min={0}
            max={100000}
            value={doc.priority ?? ""}
            onChange={(e) =>
              setDoc({
                ...doc,
                priority: e.target.value === "" ? undefined : Number(e.target.value),
              })
            }
          />
        </label>
      </div>
      <SubjectBuilder value={doc.subject} onChange={(subject) => setDoc({ ...doc, subject })} />
      <fieldset>
        <legend>Toolsets</legend>
        {toolsets.length === 0 && <p className="muted">no toolsets yet</p>}
        <div className="checks">
          {toolsets.map((t) => (
            <label key={t._id} className="check">
              <input
                type="checkbox"
                checked={doc.toolsets.includes(t._id)}
                onChange={(e) => toggle(t._id, e.target.checked)}
              />
              <code>{t._id}</code>
              {!t.enabled && <span className="badge badge-down">disabled</span>}
              <span className="muted">{t.description}</span>
            </label>
          ))}
        </div>
        {unknown.length > 0 && (
          <div className="warning">unknown toolsets: {unknown.join(", ")}</div>
        )}
      </fieldset>
      <div className="actions">
        <button
          type="submit"
          className="primary"
          disabled={save.busy || !idOk || doc.toolsets.length === 0}
        >
          {save.busy ? "Saving…" : "Save"}
        </button>
        {onDelete && (
          <button
            type="button"
            className="danger"
            disabled={save.busy}
            onClick={() => {
              if (window.confirm(`Delete grant ${doc._id}?`)) void save.run(onDelete);
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
