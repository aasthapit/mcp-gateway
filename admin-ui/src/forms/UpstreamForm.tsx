import { useState } from "react";
import schema from "../../../schemas/upstreams.schema.json";
import { CsvInput } from "../components/CsvInput";
import { ErrorView } from "../components/ErrorView";
import { SaveStatus } from "../components/SaveStatus";
import type { Upstream, WriteResult } from "../types";
import { ID_RE, useSave } from "./useSave";

// Enum options straight from the shared schema.
const props = schema.properties;
const TRANSPORTS = props.transport.enum as Upstream["transport"][];
const AUTH_TYPES = props.auth.properties.type.enum as Upstream["auth"]["type"][];
const SESSION_MODES = props.sessionMode.enum as Upstream["sessionMode"][];
const PROTOCOL_MODES = props.protocolMode.enum as NonNullable<Upstream["protocolMode"]>[];

interface Props {
  initial: Upstream | null;
  onSave(doc: Upstream): Promise<WriteResult<Upstream>>;
  onDelete?(): Promise<WriteResult<Upstream>>;
}

export const emptyUpstream = (): Upstream => ({
  _id: "",
  displayName: "",
  enabled: true,
  transport: "http",
  url: "",
  auth: { type: "none" },
  sessionMode: "per_request",
  protocolMode: "auto",
  timeoutsMs: { connect: 2000, call: 60000 },
});

const optNumber = (s: string) => (s === "" ? undefined : Number(s));

/** Form state → schema-shaped document (drops fields that do not apply). */
export function toDocument(doc: Upstream): Upstream {
  const out: Upstream = { ...doc, auth: { ...doc.auth } };
  if (out.transport === "stdio") delete out.url;
  else {
    delete out.command;
    delete out.args;
  }
  if (out.auth.type === "none") out.auth = { type: "none" };
  if (out.auth.type !== "static_header") delete out.auth.headerName;
  if (!out.auth.secretRef) delete out.auth.secretRef;
  const t = { ...out.timeoutsMs };
  if (t.connect === undefined) delete t.connect;
  if (t.call === undefined) delete t.call;
  if (Object.keys(t).length) out.timeoutsMs = t;
  else delete out.timeoutsMs;
  if (out.cacheTtlSec === undefined) delete out.cacheTtlSec;
  if (!out.tags?.length) delete out.tags;
  if (!out.args?.length) delete out.args;
  return out;
}

export function UpstreamForm({ initial, onSave, onDelete }: Props) {
  const isNew = initial === null;
  const [doc, setDoc] = useState<Upstream>(() => initial ?? emptyUpstream());
  const save = useSave<Upstream>();
  const set = (patch: Partial<Upstream>) => setDoc({ ...doc, ...patch });
  const setAuth = (patch: Partial<Upstream["auth"]>) =>
    setDoc({ ...doc, auth: { ...doc.auth, ...patch } });
  const idOk = ID_RE.test(doc._id);
  const needsSecret = doc.auth.type !== "none";

  return (
    <form
      className="editor"
      aria-label="upstream"
      onSubmit={(e) => {
        e.preventDefault();
        void save.run(() => onSave(toDocument(doc)));
      }}
    >
      <h2>{isNew ? "New upstream" : `Upstream ${doc._id}`}</h2>
      {isNew && (
        <label>
          Id <span className="muted">(also the tool namespace: tools appear as id_tool)</span>
          <input
            value={doc._id}
            placeholder="github"
            onChange={(e) => set({ _id: e.target.value })}
          />
        </label>
      )}
      <label>
        Display name
        <input value={doc.displayName} onChange={(e) => set({ displayName: e.target.value })} />
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={doc.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        Enabled
      </label>
      <div className="row">
        <label>
          Transport
          <select
            value={doc.transport}
            onChange={(e) => set({ transport: e.target.value as Upstream["transport"] })}
          >
            {TRANSPORTS.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
        {doc.transport === "stdio" ? (
          <label className="grow">
            Command <span className="muted">(local dev only; refused in prod)</span>
            <input value={doc.command ?? ""} onChange={(e) => set({ command: e.target.value })} />
          </label>
        ) : (
          <label className="grow">
            URL
            <input
              value={doc.url ?? ""}
              placeholder="http://mcp-github.mcp-upstreams.svc:8080/mcp"
              onChange={(e) => set({ url: e.target.value })}
            />
          </label>
        )}
      </div>
      {doc.transport === "stdio" && (
        <label>
          Arguments (comma-separated)
          <CsvInput value={doc.args ?? []} onList={(args) => set({ args })} />
        </label>
      )}
      <fieldset>
        <legend>Gateway → upstream auth</legend>
        <div className="row">
          <label>
            Type
            <select
              value={doc.auth.type}
              onChange={(e) => setAuth({ type: e.target.value as Upstream["auth"]["type"] })}
            >
              {AUTH_TYPES.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </label>
          {needsSecret && (
            <label className="grow">
              Secret name (secretRef)
              <input
                value={doc.auth.secretRef ?? ""}
                placeholder="upstream-github"
                autoComplete="off"
                onChange={(e) => setAuth({ secretRef: e.target.value })}
              />
            </label>
          )}
          {doc.auth.type === "static_header" && (
            <label>
              Header name
              <input
                value={doc.auth.headerName ?? ""}
                placeholder="X-Api-Key"
                onChange={(e) => setAuth({ headerName: e.target.value })}
              />
            </label>
          )}
        </div>
        {needsSecret && (
          <p className="muted">
            The <em>name</em> of a mounted secret file (/etc/gateway/upstream-secrets/&lt;name&gt;),
            never the secret value: secrets are not stored in Mongo.
          </p>
        )}
      </fieldset>
      <div className="row">
        <label>
          Session mode
          <select
            value={doc.sessionMode}
            onChange={(e) => set({ sessionMode: e.target.value as Upstream["sessionMode"] })}
          >
            {SESSION_MODES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
        <label>
          Protocol mode
          <select
            value={doc.protocolMode ?? "auto"}
            onChange={(e) => set({ protocolMode: e.target.value as Upstream["protocolMode"] })}
          >
            {PROTOCOL_MODES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
      </div>
      {doc.sessionMode === "shared" && doc.protocolMode !== "2026-07-28" && (
        <div className="warning">
          shared sessions need protocolMode 2026-07-28; the gateway falls back to per_request
        </div>
      )}
      <div className="row">
        <label>
          Connect timeout (ms)
          <input
            type="number"
            min={1}
            value={doc.timeoutsMs?.connect ?? ""}
            onChange={(e) =>
              set({ timeoutsMs: { ...doc.timeoutsMs, connect: optNumber(e.target.value) } })
            }
          />
        </label>
        <label>
          Call timeout (ms)
          <input
            type="number"
            min={1}
            value={doc.timeoutsMs?.call ?? ""}
            onChange={(e) =>
              set({ timeoutsMs: { ...doc.timeoutsMs, call: optNumber(e.target.value) } })
            }
          />
        </label>
        <label>
          Catalog cache TTL (s)
          <input
            type="number"
            min={0}
            value={doc.cacheTtlSec ?? ""}
            onChange={(e) => set({ cacheTtlSec: optNumber(e.target.value) })}
          />
        </label>
      </div>
      <label>
        Tags (comma-separated)
        <CsvInput value={doc.tags ?? []} onList={(tags) => set({ tags })} />
      </label>
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
              if (window.confirm(`Delete upstream ${doc._id}?`)) void save.run(onDelete);
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
