import { useCallback, useState } from "react";
import { AgentdError } from "../api";

/** A user action (a POST): busy while it runs, and agentd's error sentence if it
 *  fails (with 422 field messages for forms). */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<AgentdError | Error>();

  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setBusy(true);
      setError(undefined);
      try {
        return await fn(...args);
      } catch (err) {
        setError(err instanceof Error ? err : new Error(String(err)));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [fn],
  );

  const fields = error instanceof AgentdError ? error.fields : {};
  return { run, busy, error, fields, clear: () => setError(undefined) };
}
