import { useState } from "react";
import type { WriteResult } from "../types";

/** Busy/error/result state around one async write. */
export function useSave<T>() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<WriteResult<T> | null>(null);
  const run = async (fn: () => Promise<WriteResult<T>>) => {
    setBusy(true);
    setError(null);
    try {
      const r = await fn();
      setResult(r);
      return r;
    } catch (err) {
      setError(err);
      setResult(null);
      return null;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, result, run, setError };
}

export const ID_RE = /^[a-z][a-z0-9-]{0,62}$/;
