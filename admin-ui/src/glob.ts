/**
 * Python `fnmatch.fnmatchcase` semantics (what the gateway uses for allow/deny patterns):
 * `*` any run, `?` one char, `[seq]` / `[!seq]` character classes, everything else
 * literal, case-sensitive, anchored at both ends.
 */
export function globToRegExp(pattern: string): RegExp {
  let re = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i] as string;
    if (c === "*") re += ".*";
    else if (c === "?") re += ".";
    else if (c === "[") {
      let j = i + 1;
      if (pattern[j] === "!") j++;
      if (pattern[j] === "]") j++;
      while (j < pattern.length && pattern[j] !== "]") j++;
      if (j >= pattern.length) {
        re += "\\[";
      } else {
        let body = pattern.slice(i + 1, j).replace(/\\/g, "\\\\");
        if (body.startsWith("!")) body = `^${body.slice(1)}`;
        else if (body.startsWith("^")) body = `\\${body}`;
        re += `[${body}]`;
        i = j;
      }
    } else re += c.replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&");
  }
  return new RegExp(`^(?:${re})$`, "s");
}

export function isGlob(pattern: string): boolean {
  return /[*?[]/.test(pattern);
}

export function matching(pattern: string, names: readonly string[]): string[] {
  if (!pattern) return [];
  let re: RegExp;
  try {
    re = globToRegExp(pattern);
  } catch {
    return [];
  }
  return names.filter((n) => re.test(n));
}

/** Names allowed by a toolset: any allow pattern matches and no deny pattern does. */
export function effective(
  allow: readonly string[],
  deny: readonly string[],
  names: readonly string[],
): string[] {
  const a = allow.map(globToRegExp);
  const d = deny.map(globToRegExp);
  return names.filter((n) => a.some((r) => r.test(n)) && !d.some((r) => r.test(n)));
}

/** The schema's pattern for one allow/deny entry. */
export const PATTERN_RE = /^[A-Za-z0-9_*?[\]!.-]{1,200}$/;
