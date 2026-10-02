import { useCallback, useContext, useState } from "react";
import { AgentdError } from "../api";
import { BumpContext } from "./revision";

/** A user action (a POST): busy while it runs, and agentd's error sentence if it
 *  fails (with 422 field messages for forms). On success every polled view re-reads. */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<AgentdError | Error>();
  const bump = useContext(BumpContext);

  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setBusy(true);
      setError(undefined);
      try {
        const result = await fn(...args);
        bump();
        return result;
      } catch (err) {
        setError(err instanceof Error ? err : new Error(String(err)));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [fn, bump],
  );

  const fields = error instanceof AgentdError ? error.fields : {};
  return { run, busy, error, fields, clear: () => setError(undefined) };
}
