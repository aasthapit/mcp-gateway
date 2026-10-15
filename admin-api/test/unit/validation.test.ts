import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig } from "../../src/config.js";
import { HttpError } from "../../src/db.js";
import { normaliseBody } from "../../src/routes/policy.js";
import { buildValidators, COLLECTIONS } from "../../src/schemas.js";

const config = loadConfig({});
const v = buildValidators(config.schemasDir);
const seedDir = path.resolve(config.schemasDir, "../local/seed");

describe("ajv validators from ../schemas", () => {
  it.each(COLLECTIONS)("accepts every %s seed document", (collection) => {
    const docs = JSON.parse(fs.readFileSync(path.join(seedDir, `${collection}.json`), "utf8")) as unknown[];
    expect(docs.length).toBeGreaterThan(0);
    for (const doc of docs) expect(v.validate(collection, doc)).toEqual([]);
  });

  it("rejects unknown fields, bad ids and bad glob characters in toolsets", () => {
    const issues = v.validate("toolsets", { _id: "Bad_Id", enabled: true, allow: ["ok_*", "no spaces"], extra: 1 });
    const text = issues.map((i) => `${i.path} ${i.message}`).join("\n");
    expect(text).toContain("/_id must match pattern");
    expect(text).toContain("/allow/1 must match pattern");
    expect(text).toContain("must NOT have additional properties: extra");
  });

  it("reports the failing branch of a claim subject, not one error per kind", () => {
    const issues = v.validate("grants", {
      _id: "g",
      enabled: true,
      subject: { kind: "claim", path: "realm_access.roles" },
      toolsets: ["t"],
    });
    expect(issues).toContainEqual({ path: "/subject", message: "must have required property 'op'" });
    expect(issues.some((i) => i.message.includes('["anonymous"]'))).toBe(false);
  });

  it("validates every grant subject kind", () => {
    const base = { _id: "g", enabled: true, toolsets: ["t"] };
    for (const subject of [
      { kind: "anonymous" },
      { kind: "authenticated" },
      { kind: "claim", path: "groups", op: "exists" },
      { kind: "claim", path: "realm_access.roles", op: "contains", value: "x" },
      { kind: "claim", path: "tier", op: "in", value: ["gold", 2, true] },
      { kind: "client_id", value: "ci-runner" },
    ]) {
      expect(v.validate("grants", { ...base, subject }), JSON.stringify(subject)).toEqual([]);
    }
    expect(v.validate("grants", { ...base, subject: { kind: "client_id" } })).not.toEqual([]);
    expect(v.validate("grants", { ...base, subject: { kind: "anonymous", value: "x" } })).not.toEqual([]);
    expect(v.validate("grants", { ...base, toolsets: [] })).not.toEqual([]);
  });

  it("requires url for http upstreams and command for stdio", () => {
    const up = { _id: "u", displayName: "U", enabled: true, auth: { type: "none" }, sessionMode: "shared" };
    expect(v.validate("upstreams", { ...up, transport: "http" })).toContainEqual({
      path: "/",
      message: "must have required property 'url'",
    });
    expect(v.validate("upstreams", { ...up, transport: "http", url: "http://x/mcp" })).toEqual([]);
    expect(v.validate("upstreams", { ...up, transport: "stdio", command: "server" })).toEqual([]);
    expect(v.validate("upstreams", { ...up, transport: "http", url: "ftp://x" })).not.toEqual([]);
  });

  it("accepts protocolMode and keeps secrets out (secretRef is a name)", () => {
    const up = {
      _id: "gh",
      displayName: "GitHub",
      enabled: true,
      transport: "http",
      url: "https://gh/mcp",
      sessionMode: "shared",
      protocolMode: "2026-07-28",
      auth: { type: "static_bearer", secretRef: "upstream-github" },
    };
    expect(v.validate("upstreams", up)).toEqual([]);
    expect(v.validate("upstreams", { ...up, auth: { type: "static_bearer", secret: "s3cr3t" } })).not.toEqual([]);
    expect(v.validate("upstreams", { ...up, auth: { type: "static_bearer", secretRef: "has space" } })).not.toEqual([]);
  });
});

describe("normaliseBody", () => {
  it("takes _id from the URL and drops server-managed stamps", () => {
    expect(normaliseBody({ enabled: true, updatedAt: "x", updatedBy: "y" }, "a")).toEqual({ _id: "a", enabled: true });
  });
  it("rejects a mismatched _id and non-objects", () => {
    expect(() => normaliseBody({ _id: "b" }, "a")).toThrow(HttpError);
    expect(() => normaliseBody([], "a")).toThrow(HttpError);
    expect(() => normaliseBody(null, "a")).toThrow(HttpError);
  });
});
