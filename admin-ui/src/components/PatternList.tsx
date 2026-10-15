/**
 * Allow/deny editor: a list of namespaced tool names or fnmatch globs, a picker fed by the
 * gateway catalog, and a live "matches N catalog tools" hint for every entry and for the
 * pattern being typed.
 */
import { useId, useMemo, useState } from "react";
import { isGlob, matching, PATTERN_RE } from "../glob";
import type { CatalogTool } from "../types";

interface Props {
  label: string;
  value: string[];
  onChange(next: string[]): void;
  catalog: CatalogTool[];
}

export function PatternList({ label, value, onChange, catalog }: Props) {
  const [draft, setDraft] = useState("");
  const id = useId();
  const names = useMemo(() => catalog.map((t) => t.namespaced_name).sort(), [catalog]);
  const draftMatches = useMemo(() => (isGlob(draft) ? matching(draft, names) : []), [draft, names]);
  // Typing filters the catalog (substring, or the glob itself); "*" lists everything.
  const suggestions = useMemo(() => {
    if (!draft) return [];
    const q = draft.toLowerCase();
    const hits = new Set(draftMatches);
    return catalog
      .filter((t) => !value.includes(t.namespaced_name))
      .filter((t) =>
        isGlob(draft) ? hits.has(t.namespaced_name) : t.namespaced_name.toLowerCase().includes(q),
      )
      .slice(0, 10);
  }, [catalog, draft, draftMatches, value]);
  const valid = PATTERN_RE.test(draft.trim());

  const add = (raw: string) => {
    const p = raw.trim();
    if (!PATTERN_RE.test(p) || value.includes(p)) return;
    onChange([...value, p]);
    setDraft("");
  };

  return (
    <fieldset className="patterns">
      <legend>{label}</legend>
      {value.length === 0 && <p className="muted">none</p>}
      <ul className="pattern-list">
        {value.map((p) => {
          const n = matching(p, names).length;
          return (
            <li key={p}>
              <code>{p}</code>
              <span className={`hint ${n === 0 ? "hint-zero" : ""}`} data-testid={`matches-${p}`}>
                matches {n} catalog tool{n === 1 ? "" : "s"}
              </span>
              <button
                type="button"
                className="link"
                aria-label={`remove ${p}`}
                onClick={() => onChange(value.filter((x) => x !== p))}
              >
                remove
              </button>
            </li>
          );
        })}
      </ul>
      <div className="row">
        <input
          id={id}
          aria-label={`${label} pattern`}
          placeholder="type to search the catalog, or a glob such as sample-public_*"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add(draft);
            }
          }}
        />
        <button
          type="button"
          disabled={!valid || value.includes(draft.trim())}
          onClick={() => add(draft)}
        >
          Add
        </button>
      </div>
      {draft && !valid && (
        <div className="warning">only letters, digits and _ * ? [ ] ! . - are allowed</div>
      )}
      {draft && isGlob(draft) && (
        <div className="hint" data-testid="draft-hint">
          “{draft}” matches {draftMatches.length} catalog tool{draftMatches.length === 1 ? "" : "s"}
          {draftMatches.length > 0 &&
            `: ${draftMatches.slice(0, 6).join(", ")}${draftMatches.length > 6 ? ", …" : ""}`}
        </div>
      )}
      {suggestions.length > 0 && (
        <ul className="picker" aria-label={`${label} catalog`}>
          {suggestions.map((t) => (
            <li key={t.namespaced_name}>
              <button type="button" className="pick" onClick={() => add(t.namespaced_name)}>
                <code>{t.namespaced_name}</code>
                <span className="muted">{t.description ?? ""}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  );
}
