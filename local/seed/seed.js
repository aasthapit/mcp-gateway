// Load local/seed/*.json into the policy collections and bump meta.version.
// Run inside the mongo-init container:  make seed
// Writes use w:majority so a gateway reading with readConcern majority sees them immediately.
(() => {
  const fs = require("fs");
  const dbName = process.env.MONGO_DB || "mcp_gateway";
  const seedDir = process.env.SEED_DIR || "/seed";
  const policyDb = db.getSiblingDB(dbName);
  const wc = { w: "majority", wtimeout: 10000 };
  const now = new Date();
  const actor = process.env.SEED_ACTOR || "seed";

  const collections = ["upstreams", "toolsets", "grants"];
  const summary = {};
  for (const name of collections) {
    const docs = JSON.parse(fs.readFileSync(`${seedDir}/${name}.json`, "utf8"));
    const ops = docs.map((doc) => ({
      replaceOne: {
        filter: { _id: doc._id },
        replacement: { ...doc, updatedAt: now, updatedBy: actor },
        upsert: true,
      },
    }));
    const res = policyDb.getCollection(name).bulkWrite(ops, { ordered: true, writeConcern: wc });
    summary[name] = {
      upserted: res.upsertedCount,
      modified: res.modifiedCount,
      matched: res.matchedCount,
    };
  }

  const meta = policyDb.meta.findOneAndUpdate(
    { _id: "policy" },
    { $inc: { version: 1 }, $set: { updatedAt: now, updatedBy: actor } },
    { upsert: true, returnDocument: "after", writeConcern: wc },
  );
  summary.meta = { version: meta.version };
  print(JSON.stringify(summary, null, 2));
})();
