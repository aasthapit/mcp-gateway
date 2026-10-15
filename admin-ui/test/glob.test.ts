import { describe, expect, it } from "vitest";
import { effective, globToRegExp, matching } from "../src/glob";

const names = [
  "sample-public_time_now",
  "sample-public_echo",
  "sample-private_secret_lookup",
  "a.b_c",
];

describe("fnmatch-compatible globs", () => {
  it.each([
    ["sample-public_*", ["sample-public_time_now", "sample-public_echo"]],
    ["*_echo", ["sample-public_echo"]],
    ["sample-p?????_*", ["sample-public_time_now", "sample-public_echo"]],
    ["sample-pr[!u]*", ["sample-private_secret_lookup"]],
    ["sample-pu[a-c]lic_echo", ["sample-public_echo"]],
    ["a.b_c", ["a.b_c"]],
    ["a?b_c", ["a.b_c"]],
    ["*", names],
    ["Sample-*", []],
  ])("%s", (pattern, expected) => {
    expect(matching(pattern, names)).toEqual(expected);
  });

  it("treats regex metacharacters and an unclosed [ literally", () => {
    expect(globToRegExp("a.b").test("axb")).toBe(false);
    expect(globToRegExp("x[y").test("x[y")).toBe(true);
  });

  it("deny wins over allow", () => {
    expect(effective(["sample-public_*"], ["sample-public_echo"], names)).toEqual([
      "sample-public_time_now",
    ]);
  });
});
