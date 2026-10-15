/**
 * CRUD over upstreams / toolsets / grants, plus /api/meta and /api/history.
 * Every mutation goes through `policyWrite` (one majority transaction: document,
 * meta.version bump, policy_history entry).
 */
import type { FastifyInstance } from "fastify";
import type { Document } from "mongodb";
import type { AppDeps } from "../app.js";
import { principalOf } from "../auth.js";
import { currentVersion, HISTORY, HttpError, META_ID, type PolicyDoc, policyWrite } from "../db.js";
import type { Metrics } from "../metrics.js";
import { deleteGuard, missingToolsets } from "../references.js";
import { type Collection, isCollection, SERVER_FIELDS } from "../schemas.js";

const ID = /^[a-z][a-z0-9-]{0,62}$/;
/** GET /api/upstreams/health is a static route; an upstream with this id could not be read. */
const RESERVED_UPSTREAM_IDS = new Set(["health", "reload"]);
const HISTORY_MAX = 500;

interface Params {
  collection: string;
  id: string;
}

function collectionParam(value: string): Collection {
  if (!isCollection(value)) throw new HttpError(404, { error: `unknown collection ${value}` });
  return value;
}

function idParam(value: string): string {
  if (!ID.test(value))
    throw new HttpError(400, { error: `invalid id ${JSON.stringify(value)} (want ${ID.source})` });
  return value;
}

/** Request body → the document to store: server stamps removed, `_id` taken from the URL. */
export function normaliseBody(body: unknown, id: string): PolicyDoc {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new HttpError(400, { error: "body must be a JSON object" });
  }
  const { _id = id, ...rest } = body as Document;
  if (_id !== id)
    throw new HttpError(400, {
      error: `body _id ${JSON.stringify(_id)} does not match URL id ${id}`,
    });
  for (const f of SERVER_FIELDS) delete rest[f];
  return { _id: id, ...rest };
}

export function registerPolicyRoutes(
  app: FastifyInstance,
  deps: AppDeps & { metrics: Metrics },
): void {
  const { db, client } = deps.mongo;
  const { validators, metrics } = deps;

  app.get("/api/meta", async () => {
    const meta = await db.collection<{ _id: string }>("meta").findOne({ _id: META_ID });
    return meta ?? { _id: META_ID, version: 0 };
  });

  app.get<{ Querystring: { collection?: string; id?: string; limit?: string } }>(
    "/api/history",
    async (request) => {
      const { collection, id, limit } = request.query;
      const filter: Document = {};
      if (collection) filter.collection = collectionParam(collection);
      if (id) filter.id = idParam(id);
      const n = Math.min(Math.max(Number.parseInt(limit ?? "50", 10) || 50, 1), HISTORY_MAX);
      const items = await db
        .collection(HISTORY)
        .find(filter)
        .sort({ at: -1, _id: -1 })
        .limit(n)
        .toArray();
      return { items };
    },
  );

  app.get<{ Params: Pick<Params, "collection"> }>("/api/:collection", async (request) => {
    const collection = collectionParam(request.params.collection);
    const [items, version] = await Promise.all([
      db.collection<PolicyDoc>(collection).find().sort({ _id: 1 }).toArray(),
      currentVersion(db),
    ]);
    return { items, version };
  });

  app.get<{ Params: Params }>("/api/:collection/:id", async (request) => {
    const collection = collectionParam(request.params.collection);
    const id = idParam(request.params.id);
    const doc = await db.collection<PolicyDoc>(collection).findOne({ _id: id });
    if (!doc) throw new HttpError(404, { error: `${collection}/${id} not found` });
    return doc;
  });

  app.put<{ Params: Params }>("/api/:collection/:id", async (request) => {
    const collection = collectionParam(request.params.collection);
    const id = idParam(request.params.id);
    if (collection === "upstreams" && RESERVED_UPSTREAM_IDS.has(id)) {
      throw new HttpError(400, { error: `upstream id ${id} is reserved by the API` });
    }
    const doc = normaliseBody(request.body, id);
    const issues = validators.validate(collection, doc);
    if (issues.length) {
      throw new HttpError(400, { error: `invalid ${collection} document`, details: issues });
    }
    const actor = principalOf(request);
    const result = await policyWrite(client, db, {
      collection,
      id,
      doc,
      actor: { sub: actor.sub, name: actor.name },
    });
    metrics.writes.inc({ collection, op: result.op });
    if (result.op !== "noop") metrics.policyVersion.set(result.version);
    request.log.info(
      { audit: { collection, id, op: result.op, version: result.version, actor: actor.sub } },
      "policy write",
    );
    const warnings =
      collection === "grants"
        ? (await missingToolsets(db, doc)).map(
            (t) => `toolset ${t} does not exist (the grant has no effect for it)`,
          )
        : [];
    return { op: result.op, version: result.version, document: result.after, warnings };
  });

  app.delete<{ Params: Params }>("/api/:collection/:id", async (request) => {
    const collection = collectionParam(request.params.collection);
    const id = idParam(request.params.id);
    const actor = principalOf(request);
    const result = await policyWrite(client, db, {
      collection,
      id,
      actor: { sub: actor.sub, name: actor.name },
      precheck: deleteGuard(collection, id),
    });
    metrics.writes.inc({ collection, op: result.op });
    metrics.policyVersion.set(result.version);
    request.log.info(
      { audit: { collection, id, op: "delete", version: result.version, actor: actor.sub } },
      "policy write",
    );
    return { op: result.op, version: result.version, document: result.before };
  });
}
