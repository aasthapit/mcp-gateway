import { ApiError } from "../api";

export function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status}: ${err.message}`;
  return err instanceof Error ? err.message : String(err);
}

export function ErrorView({ error }: { error: unknown }) {
  if (!error) return null;
  const details = error instanceof ApiError ? error.body.details : undefined;
  const refs = error instanceof ApiError ? error.body.referencedBy : undefined;
  return (
    <div className="error" role="alert">
      <strong>{errorText(error)}</strong>
      {Array.isArray(details) && (
        <ul>
          {details.map((d) => (
            <li key={`${d.path} ${d.message}`}>
              <code>{d.path}</code> {d.message}
            </li>
          ))}
        </ul>
      )}
      {typeof details === "string" && <div>{details}</div>}
      {refs && (
        <ul>
          {Object.entries(refs).map(([coll, ids]) => (
            <li key={coll}>
              referenced by {coll}:{" "}
              {ids.map((id) => (
                <code key={id}>{id} </code>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
