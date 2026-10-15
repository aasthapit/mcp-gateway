/**
 * Mongo access and the one write path every admin mutation goes through (ADR 0011):
 *
 *   one transaction, writeConcern majority, readConcern snapshot:
 *     1. read the current document (`before`)
 *     2. optional precheck inside the transaction (reference checks for deletes)
 *     3. replace (upsert) or delete the document, stamping updatedAt / updatedBy
 *     4. $inc meta.version (+ updatedAt / updatedBy) on { _id: "policy" }
 *     5. append { collection, id, op, actor, at, version, before, after } to policy_history
 *
 * The gateway's change stream sees the commit as one unit; its 30 s poll compares
 * meta.version, so a write that skipped step 4 would only be noticed by the stream.
 */
import { type ClientSession, type Db, type Document, MongoClient } from "mongodb";
import type { Collection } from "./schemas.js";

export const META_ID = "policy";
export const HISTORY = "policy_history";

export type PolicyDoc = Document & { _id: string };

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly body: Record<string, unknown>,
  ) {
    super(String(body.error ?? status));
  }
}

export interface Actor {
  /** Caller `sub`: stored as updatedBy and as the history actor. */
  sub: string;
  /** Human-readable name (preferred_username) for the history table; informational. */
  name?: string | undefined;
}

export interface WriteRequest {
  collection: Collection;
  id: string;
  actor: Actor;
  /** The full document for a put (without updatedAt/updatedBy); omitted for a delete. */
  doc?: PolicyDoc | undefined;
  /** Runs inside the transaction before the write; throw HttpError to abort. */
  precheck?: ((session: ClientSession, db: Db) => Promise<void>) | undefined;
}

export interface WriteResult {
  op: "create" | "update" | "delete" | "noop";
  version: number;
  at: Date;
  before: PolicyDoc | null;
  after: PolicyDoc | null;
}

export const TXN_OPTIONS = {
  readConcern: { level: "snapshot" as const },
  writeConcern: { w: "majority" as const, wtimeoutMS: 10_000 },
  readPreference: "primary" as const,
};

export async function connectMongo(uri: string, dbName: string): Promise<{ client: MongoClient; db: Db }> {
  const client = new MongoClient(uri, {
    serverSelectionTimeoutMS: 5_000,
    writeConcern: { w: "majority" },
    readConcern: { level: "majority" },
    appName: "mcp-admin-api",
  });
  await client.connect();
  return { client, db: client.db(dbName) };
}

export async function ensureIndexes(db: Db): Promise<void> {
  await db.collection(HISTORY).createIndexes([
    { key: { at: -1 }, name: "at_desc" },
    { key: { collection: 1, id: 1, at: -1 }, name: "collection_id_at" },
  ]);
}

/** Field-wise equality of two policy docs, ignoring the server-managed stamps. */
export function sameContent(a: Document | null, b: Document | null): boolean {
  if (!a || !b) return a === b;
  const strip = ({ updatedAt: _a, updatedBy: _b, ...rest }: Document) => rest;
  return canonical(strip(a)) === canonical(strip(b));
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value instanceof Date) return JSON.stringify(value.toISOString());
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export async function currentVersion(db: Db, session?: ClientSession): Promise<number> {
  const meta = await db.collection<{ _id: string; version?: number }>("meta").findOne({ _id: META_ID }, { session });
  return Number(meta?.version ?? 0);
}

/** The single write path: see the module comment. Retries transient transaction errors. */
export async function policyWrite(client: MongoClient, db: Db, req: WriteRequest): Promise<WriteResult> {
  const { collection, id, actor, doc } = req;
  if (doc && doc._id !== id) throw new HttpError(400, { error: "body _id does not match the URL id" });
  return client.withSession((session) =>
    session.withTransaction(async (): Promise<WriteResult> => {
      const at = new Date();
      const coll = db.collection<PolicyDoc>(collection);
      const before = await coll.findOne({ _id: id }, { session });
      if (!doc && !before) throw new HttpError(404, { error: `${collection}/${id} not found` });
      if (req.precheck) await req.precheck(session, db);

      if (doc && sameContent(before, doc)) {
        return { op: "noop", version: await currentVersion(db, session), at, before, after: before };
      }

      let after: PolicyDoc | null = null;
      if (doc) {
        after = { ...doc, _id: id, updatedAt: at, updatedBy: actor.sub };
        await coll.replaceOne({ _id: id }, after, { upsert: true, session });
      } else {
        await coll.deleteOne({ _id: id }, { session });
      }

      const meta = await db
        .collection<{ _id: string; version: number }>("meta")
        .findOneAndUpdate(
          { _id: META_ID },
          { $inc: { version: 1 }, $set: { updatedAt: at, updatedBy: actor.sub } },
          { upsert: true, returnDocument: "after", session },
        );
      const version = Number(meta?.version ?? 0);
      const op = !doc ? "delete" : before ? "update" : "create";

      await db.collection(HISTORY).insertOne(
        {
          collection,
          id,
          op,
          actor: actor.sub,
          actorName: actor.name ?? null,
          at,
          version,
          before,
          after,
        },
        { session },
      );
      return { op, version, at, before, after };
    }, TXN_OPTIONS),
  );
}
