/** Grant subject: anonymous | authenticated | claim {path, op, value} | client_id. */
import { useState } from "react";
import type { ClaimOp, Scalar, Subject } from "../types";

const KINDS: { kind: Subject["kind"]; label: string; help: string }[] = [
  {
    kind: "anonymous",
    label: "Anonymous",
    help: "Matches every caller, with or without a token (the guest floor).",
  },
  { kind: "authenticated", label: "Authenticated", help: "Any caller with a valid token." },
  { kind: "claim", label: "Claim", help: "A dotted path into the verified token claims." },
  {
    kind: "client_id",
    label: "Client id",
    help: "The token's client (azp / client_id), e.g. a CI robot.",
  },
];
const OPS: { op: ClaimOp; label: string }[] = [
  { op: "contains", label: "contains (claim is an array or space-separated string)" },
  { op: "eq", label: "equals" },
  { op: "in", label: "is one of" },
  { op: "exists", label: "exists" },
];
const PATH_HINTS = ["realm_access.roles", "roles", "groups", "scp", "scope", "azp", "email"];
type ValueType = "string" | "number" | "boolean";

function typeOf(v: Scalar | Scalar[] | undefined): ValueType {
  const first = Array.isArray(v) ? v[0] : v;
  return typeof first === "number" ? "number" : typeof first === "boolean" ? "boolean" : "string";
}

function parseScalar(raw: string, t: ValueType): Scalar {
  if (t === "number") return Number(raw);
  if (t === "boolean") return raw.trim().toLowerCase() === "true";
  return raw;
}

function display(v: Scalar | Scalar[] | undefined): string {
  if (v === undefined) return "";
  return Array.isArray(v) ? v.join(", ") : String(v);
}

export function SubjectBuilder({
  value,
  onChange,
}: {
  value: Subject;
  onChange(s: Subject): void;
}) {
  const setKind = (kind: Subject["kind"]) => {
    if (kind === value.kind) return;
    if (kind === "claim") onChange({ kind, path: "realm_access.roles", op: "contains", value: "" });
    else if (kind === "client_id") onChange({ kind, value: "" });
    else onChange({ kind });
  };
  return (
    <fieldset className="subject">
      <legend>Subject</legend>
      <div className="kinds" role="radiogroup" aria-label="subject kind">
        {KINDS.map((k) => (
          <label key={k.kind} className={value.kind === k.kind ? "kind active" : "kind"}>
            <input
              type="radio"
              name="subject-kind"
              checked={value.kind === k.kind}
              onChange={() => setKind(k.kind)}
            />
            {k.label}
          </label>
        ))}
      </div>
      <p className="muted">{KINDS.find((k) => k.kind === value.kind)?.help}</p>
      {value.kind === "claim" && <ClaimFields value={value} onChange={onChange} />}
      {value.kind === "client_id" && (
        <label>
          Client id
          <input
            value={value.value}
            placeholder="ci-runner"
            onChange={(e) => onChange({ kind: "client_id", value: e.target.value })}
          />
        </label>
      )}
    </fieldset>
  );
}

function ClaimFields({
  value,
  onChange,
}: {
  value: Extract<Subject, { kind: "claim" }>;
  onChange(s: Subject): void;
}) {
  const [vtype, setVtype] = useState<ValueType>(() => typeOf(value.value));
  // The raw text is kept so "a," or "-" can be typed; the subject gets the parsed value.
  const [raw, setRaw] = useState(() => display(value.value));
  const parse = (text: string, op: ClaimOp, t: ValueType): Scalar | Scalar[] =>
    op === "in"
      ? text
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => parseScalar(s, t))
      : parseScalar(text.trim(), t);
  const setValue = (text: string, t: ValueType = vtype) => {
    setRaw(text);
    setVtype(t);
    onChange({ ...value, value: parse(text, value.op, t) });
  };
  const setOp = (op: ClaimOp) => {
    if (op === "exists") {
      const { value: _v, ...rest } = value;
      onChange({ ...rest, op });
    } else onChange({ ...value, op, value: parse(raw, op, vtype) });
  };
  return (
    <div className="claim">
      <label>
        Claim path
        <input
          list="claim-paths"
          value={value.path}
          onChange={(e) => onChange({ ...value, path: e.target.value })}
        />
        <datalist id="claim-paths">
          {PATH_HINTS.map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
      </label>
      <label>
        Operator
        <select value={value.op} onChange={(e) => setOp(e.target.value as ClaimOp)}>
          {OPS.map((o) => (
            <option key={o.op} value={o.op}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {value.op !== "exists" && (
        <div className="row">
          <label className="grow">
            {value.op === "in" ? "Values (comma-separated)" : "Value"}
            <input
              aria-label="claim value"
              value={raw}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
          <label>
            Type
            <select
              aria-label="value type"
              value={vtype}
              onChange={(e) => setValue(raw, e.target.value as ValueType)}
            >
              <option value="string">string</option>
              <option value="number">number</option>
              <option value="boolean">boolean</option>
            </select>
          </label>
        </div>
      )}
      <p className="muted">
        Matches when <code>{value.path || "…"}</code>{" "}
        {value.op === "exists" ? "is present" : `${value.op} ${JSON.stringify(value.value ?? "")}`}
      </p>
    </div>
  );
}
