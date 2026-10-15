import { diffLines } from "diff";

/** Stable, key-sorted pretty JSON so diffs show content changes, not key order. */
export function pretty(value: unknown): string {
  const sort = (v: unknown): unknown =>
    Array.isArray(v)
      ? v.map(sort)
      : v && typeof v === "object"
        ? Object.fromEntries(
            Object.entries(v as Record<string, unknown>)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([k, x]) => [k, sort(x)]),
          )
        : v;
  return value == null ? "" : `${JSON.stringify(sort(value), null, 2)}\n`;
}

export function JsonDiff({ before, after }: { before: unknown; after: unknown }) {
  const parts = diffLines(pretty(before), pretty(after));
  return (
    <pre className="diff" aria-label="before/after diff">
      {parts.map((p, i) => (
        <span key={i} className={p.added ? "add" : p.removed ? "del" : "same"}>
          {p.value
            .replace(/\n$/, "")
            .split("\n")
            .map((line) => `${p.added ? "+" : p.removed ? "-" : " "} ${line}`)
            .join("\n")}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}
