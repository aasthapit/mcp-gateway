/**
 * ajv validators compiled from `../schemas/*.schema.json`, the same files that generate the
 * Mongo `$jsonSchema` validators and that the gateway's pydantic models mirror. Read at
 * startup (no copy step), so the three consumers cannot drift apart.
 */
import fs from "node:fs";
import path from "node:path";
import type { ErrorObject, ValidateFunction } from "ajv";
import ajvDraft04 from "ajv-draft-04";
import ajvFormats from "ajv-formats";

// CommonJS packages: under NodeNext the class/plugin is the `default` property.
const Ajv04 = ajvDraft04.default;
const addFormats = ajvFormats.default;

export const COLLECTIONS = ["upstreams", "toolsets", "grants"] as const;
export type Collection = (typeof COLLECTIONS)[number];

export function isCollection(value: string): value is Collection {
  return (COLLECTIONS as readonly string[]).includes(value);
}

/** Fields the API owns: stripped from request bodies, set on every write. */
export const SERVER_FIELDS = ["updatedAt", "updatedBy"] as const;

export interface ValidationIssue {
  path: string;
  message: string;
}

export interface Validators {
  validate(collection: Collection, doc: unknown): ValidationIssue[];
  schema(collection: Collection): Record<string, unknown>;
}

/**
 * For a failed oneOf/anyOf, drop the branches whose discriminator did not match (they report
 * an `enum` error, e.g. `subject.kind` is not "anonymous"), so a claim subject without `op`
 * reports "must have required property 'op'" instead of one error per subject kind.
 */
export function pruneBranchErrors(errors: ErrorObject[]): ErrorObject[] {
  let out = errors;
  for (const parent of errors.filter((e) => e.keyword === "oneOf" || e.keyword === "anyOf")) {
    const prefix = `${parent.schemaPath}/`;
    const branches = new Map<string, ErrorObject[]>();
    for (const e of out) {
      if (!e.schemaPath.startsWith(prefix)) continue;
      const branch = e.schemaPath.slice(prefix.length).split("/")[0] ?? "";
      branches.set(branch, [...(branches.get(branch) ?? []), e]);
    }
    const mismatched = [...branches.entries()].filter(([, errs]) => errs.some((e) => e.keyword === "enum"));
    if (mismatched.length === 0 || mismatched.length === branches.size) continue;
    const drop = new Set(mismatched.flatMap(([, errs]) => errs));
    out = out.filter((e) => !drop.has(e));
  }
  return out;
}

function formatErrors(errors: ErrorObject[] | null | undefined): ValidationIssue[] {
  const seen = new Set<string>();
  const out: ValidationIssue[] = [];
  for (const e of pruneBranchErrors(errors ?? [])) {
    const extra =
      e.keyword === "additionalProperties"
        ? `: ${String((e.params as { additionalProperty?: string }).additionalProperty)}`
        : e.keyword === "enum"
          ? `: ${JSON.stringify((e.params as { allowedValues?: unknown }).allowedValues)}`
          : "";
    const issue = { path: e.instancePath || "/", message: `${e.message ?? e.keyword}${extra}` };
    const key = `${issue.path} ${issue.message}`;
    if (!seen.has(key)) {
      seen.add(key);
      out.push(issue);
    }
  }
  return out;
}

export function loadSchemas(dir: string): Record<Collection, Record<string, unknown>> {
  const out = {} as Record<Collection, Record<string, unknown>>;
  for (const name of COLLECTIONS) {
    const file = path.join(dir, `${name}.schema.json`);
    if (!fs.existsSync(file)) throw new Error(`schema not found: ${file} (set ADMIN_SCHEMAS_DIR)`);
    out[name] = JSON.parse(fs.readFileSync(file, "utf8")) as Record<string, unknown>;
  }
  return out;
}

export function buildValidators(dir: string): Validators {
  const schemas = loadSchemas(dir);
  // The files are draft-04 (Mongo's $jsonSchema dialect); draft-04 identifies schemas by
  // `id`, so the `$id` URL is dropped before compiling. strictRequired is off because the
  // upstream schema says "transport http needs url" as `anyOf: [{properties, required}]`.
  const ajv = new Ajv04({ allErrors: true, strict: true, strictTypes: false, strictRequired: false, allowUnionTypes: true });
  addFormats(ajv);
  const compiled = {} as Record<Collection, ValidateFunction>;
  for (const name of COLLECTIONS) {
    const { $id: _ignored, ...schema } = schemas[name];
    compiled[name] = ajv.compile(schema);
  }
  return {
    validate(collection, doc) {
      const fn = compiled[collection];
      return fn(doc) ? [] : formatErrors(fn.errors);
    },
    schema: (collection) => schemas[collection],
  };
}
