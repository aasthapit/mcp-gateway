/**
 * policyWrite against the live local replica set (transactions need one; mongodb-memory-
 * server is standalone). Uses a throwaway database so no gateway sees these writes.
 * Skips when Mongo is unreachable.
 */
import { randomBytes } from "node:crypto";
import { type Db, MongoClient } from "mongodb";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { HISTORY, HttpError, META_ID, policyWrite } from "../../src/db.js";
import { deleteGuard } from "../../src/references.js";
import { MONGO_URI } from "../../scripts/stack.js";

let client: MongoClient | undefined;
try {
  client = new MongoClient(MONGO_URI, { serverSelectionTimeoutMS: 1_500 });
  await client.connect();
  await client.db("admin").command({ ping: 1 });
} catch {
  await client?.close().catch(() => {});
  client = undefined;
}

describe.skipIf(!client)("policyWrite (live Mongo)", () => {
  const c = client as MongoClient;
  let db: Db;
  const actor = { sub: "sub-1", name: "admin1" };
  const version = async () => Number((await db.collection<{ _id: string; version: number }>("meta").findOne({ _id: META_ID }))?.version ?? 0);

  beforeAll(async () => {
    db = c.db(`mcp_admin_test_${randomBytes(4).toString("hex")}`);
    // Collections must exist before a transaction writes to them on older servers.
    for (const name of ["toolsets", "grants", "upstreams", "meta", HISTORY]) await db.createCollection(name);
  });
  afterAll(async () => {
    await db?.dropDatabase();
    await c.close();
  });

  it("writes the document, bumps meta.version and appends history in one transaction", async () => {
    const doc = { _id: "t1", enabled: true, allow: ["a_*"] };
    const r1 = await policyWrite(c, db, { collection: "toolsets", id: "t1", actor, doc });
    expect(r1).toMatchObject({ op: "create", version: 1, before: null });
    const stored = await db.collection("toolsets").findOne({ _id: "t1" as never });
    expect(stored).toMatchObject({ ...doc, updatedBy: "sub-1" });
    expect(stored?.updatedAt).toBeInstanceOf(Date);
    expect(await db.collection("meta").findOne({ _id: META_ID as never })).toMatchObject({ version: 1, updatedBy: "sub-1" });

    const r2 = await policyWrite(c, db, { collection: "toolsets", id: "t1", actor, doc: { ...doc, allow: ["b"] } });
    expect(r2).toMatchObject({ op: "update", version: 2 });
    const history = await db.collection(HISTORY).find({ id: "t1" }).sort({ version: 1 }).toArray();
    expect(history).toHaveLength(2);
    expect(history[1]).toMatchObject({
      collection: "toolsets",
      id: "t1",
      op: "update",
      actor: "sub-1",
      actorName: "admin1",
      version: 2,
      before: { allow: ["a_*"] },
      after: { allow: ["b"] },
    });
  });

  it("an unchanged PUT is a noop: no version bump, no history entry", async () => {
    const before = await version();
    const n = await db.collection(HISTORY).countDocuments();
    const r = await policyWrite(c, db, { collection: "toolsets", id: "t1", actor, doc: { _id: "t1", enabled: true, allow: ["b"] } });
    expect(r.op).toBe("noop");
    expect(await version()).toBe(before);
    expect(await db.collection(HISTORY).countDocuments()).toBe(n);
  });

  it("a failed precheck aborts everything (no delete, no bump, no history)", async () => {
    await policyWrite(c, db, {
      collection: "grants",
      id: "g1",
      actor,
      doc: { _id: "g1", enabled: true, subject: { kind: "anonymous" }, toolsets: ["t1"] },
    });
    const before = await version();
    const n = await db.collection(HISTORY).countDocuments();
    const err = await policyWrite(c, db, { collection: "toolsets", id: "t1", actor, precheck: deleteGuard("toolsets", "t1") }).catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(HttpError);
    expect((err as HttpError).status).toBe(409);
    expect((err as HttpError).body.referencedBy).toEqual({ grants: ["g1"] });
    expect(await db.collection("toolsets").countDocuments({ _id: "t1" as never })).toBe(1);
    expect(await version()).toBe(before);
    expect(await db.collection(HISTORY).countDocuments()).toBe(n);
  });

  it("deletes record the before image; deleting a missing doc is a 404", async () => {
    const r = await policyWrite(c, db, { collection: "grants", id: "g1", actor });
    expect(r).toMatchObject({ op: "delete", after: null, before: { _id: "g1" } });
    const err = await policyWrite(c, db, { collection: "grants", id: "g1", actor }).catch((e: unknown) => e);
    expect((err as HttpError).status).toBe(404);
  });

  it("concurrent writers each get their own version (transient conflicts are retried)", async () => {
    const start = await version();
    const results = await Promise.all(
      Array.from({ length: 8 }, (_, i) =>
        policyWrite(c, db, { collection: "toolsets", id: `p${i}`, actor, doc: { _id: `p${i}`, enabled: true, allow: [] } }),
      ),
    );
    const versions = results.map((r) => r.version).sort((a, b) => a - b);
    expect(versions).toEqual(Array.from({ length: 8 }, (_, i) => start + i + 1));
    expect(await version()).toBe(start + 8);
  });
});
