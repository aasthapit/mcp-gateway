import type { WriteResult } from "../types";

/** "Saved … meta.version N" line under a form after every write. */
export function SaveStatus({ result }: { result: WriteResult<unknown> | null }) {
  if (!result) return null;
  const verb = { create: "Created", update: "Saved", delete: "Deleted", noop: "No changes" }[
    result.op
  ];
  return (
    <div className="saved" role="status">
      {verb} — meta.version <strong data-testid="meta-version">{result.version}</strong>
      {result.op === "noop" && " (unchanged, no new version)"}
      {result.warnings?.map((w) => (
        <div key={w} className="warning">
          ⚠ {w}
        </div>
      ))}
    </div>
  );
}
