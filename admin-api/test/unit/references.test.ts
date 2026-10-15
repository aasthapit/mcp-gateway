import { describe, expect, it } from "vitest";
import { sameContent } from "../../src/db.js";
import { patternReferencesUpstream, toolsetReferencesUpstream } from "../../src/references.js";

describe("upstream references", () => {
  it("counts patterns that name the upstream's namespace", () => {
    expect(patternReferencesUpstream("github_search", "github")).toBe(true);
    expect(patternReferencesUpstream("github_*", "github")).toBe(true);
    expect(patternReferencesUpstream("github-enterprise_search", "github")).toBe(false);
    expect(patternReferencesUpstream("*", "github")).toBe(false);
    expect(patternReferencesUpstream("git*", "github")).toBe(false);
  });
  it("looks at allow and deny", () => {
    expect(toolsetReferencesUpstream({ _id: "t", allow: [], deny: ["gh_x"] }, "gh")).toBe(true);
    expect(toolsetReferencesUpstream({ _id: "t", allow: ["other_x"] }, "gh")).toBe(false);
  });
});

describe("sameContent", () => {
  it("ignores updatedAt/updatedBy and key order", () => {
    const a = { _id: "x", allow: ["a"], enabled: true, updatedAt: new Date(1), updatedBy: "u1" };
    const b = { enabled: true, _id: "x", allow: ["a"] };
    expect(sameContent(a, b)).toBe(true);
    expect(sameContent(a, { ...b, allow: ["b"] })).toBe(false);
    expect(sameContent(null, b)).toBe(false);
  });
});
