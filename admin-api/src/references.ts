/**
 * Delete guards: a toolset named by a grant, or an upstream named by a toolset pattern,
 * cannot be deleted (409 with the referencing ids). Checked inside the write transaction.
 *
 * An allow/deny entry references upstream `u` when it starts with the literal namespace
 * `u_` (`u_search`, `u_*`). Namespace-wide globs such as `*` or `sample-*` are not counted:
 * they keep working after the upstream is gone and would otherwise block every delete.
 * Upstream ids cannot contain `_`, so the prefix is unambiguous.
 */
import type { ClientSession, Db } from "mongodb";
import { HttpError, type PolicyDoc } from "./db.js";
import type { Collection } from "./schemas.js";

export function patternReferencesUpstream(pattern: string, upstreamId: string): boolean {
  return pattern.startsWith(`${upstreamId}_`);
}

export function toolsetReferencesUpstream(toolset: PolicyDoc, upstreamId: string): boolean {
  const entries = [...((toolset.allow as string[]) ?? []), ...((toolset.deny as string[]) ?? [])];
  return entries.some((p) => patternReferencesUpstream(p, upstreamId));
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export interface References {
  grants?: string[];
  toolsets?: string[];
}

export async function findReferences(
  db: Db,
  collection: Collection,
  id: string,
  session?: ClientSession,
): Promise<References> {
  if (collection === "toolsets") {
    const grants = await db
      .collection<PolicyDoc>("grants")
      .find({ toolsets: id }, { session, projection: { _id: 1 } })
      .toArray();
    return grants.length ? { grants: grants.map((g) => g._id).sort() } : {};
  }
  if (collection === "upstreams") {
    const prefix = new RegExp(`^${escapeRegex(id)}_`);
    const toolsets = await db
      .collection<PolicyDoc>("toolsets")
      .find({ $or: [{ allow: prefix }, { deny: prefix }] }, { session })
      .toArray();
    const ids = toolsets.filter((t) => toolsetReferencesUpstream(t, id)).map((t) => t._id);
    return ids.length ? { toolsets: ids.sort() } : {};
  }
  return {};
}

export function deleteGuard(collection: Collection, id: string) {
  return async (session: ClientSession, db: Db): Promise<void> => {
    const refs = await findReferences(db, collection, id, session);
    if (Object.keys(refs).length) {
      throw new HttpError(409, {
        error: `${collection}/${id} is still referenced`,
        referencedBy: refs,
      });
    }
  };
}

/** Toolsets a grant names that do not exist (a warning on PUT, not an error). */
export async function missingToolsets(db: Db, grant: PolicyDoc): Promise<string[]> {
  const names = (grant.toolsets as string[]) ?? [];
  if (!names.length) return [];
  const found = await db
    .collection<PolicyDoc>("toolsets")
    .find({ _id: { $in: names } }, { projection: { _id: 1 } })
    .toArray();
  const have = new Set(found.map((t) => t._id));
  return names.filter((n) => !have.has(n));
}
