// The one polling loop every view uses. agentd has no push channel, so lists and
// details are re-read while they are visible:
//   - only while the window is visible (Page Visibility API) and the view is mounted;
//   - one request at a time; a superseded or unmounted one is aborted;
//   - after the connection fails, waits longer each time (up to 30 s);
//   - re-reads at once when /events reports a change (EventsProvider's revision).

import { useCallback, useContext, useEffect, useEffectEvent, useState } from "react";
import { AgentdError } from "../api";
import { RevisionContext } from "./revision";
import { useVisible } from "./useVisibility";

export interface Polled<T> {
  data: T | undefined;
  error: AgentdError | Error | undefined;
  loading: boolean;
  /** Read again now (after an action). */
  refresh: () => void;
}

export interface PollOptions {
  /** ms between reads while visible (default 4000). 0: read once, and on refresh/revision. */
  interval?: number;
  /** false: don't read at all (e.g. nothing selected). */
  enabled?: boolean;
  /** Re-read when /events reports any change (default true). */
  followEvents?: boolean;
}

export const MAX_BACKOFF = 30_000;

/** Same subject: the deps are the same values, in order. */
export function sameDeps(a: ReadonlyArray<unknown>, b: ReadonlyArray<unknown>): boolean {
  return a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
}

export function nextDelay(interval: number, failures: number): number {
  if (failures === 0) return interval;
  return Math.min(MAX_BACKOFF, Math.max(interval, 2000) * 2 ** (failures - 1));
}

export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: ReadonlyArray<unknown>,
  { interval = 4000, enabled = true, followEvents = true }: PollOptions = {},
): Polled<T> {
  const visible = useVisible();
  const revision = useContext(RevisionContext);
  const [data, setData] = useState<T>();
  const [error, setError] = useState<AgentdError | Error>();
  const [loading, setLoading] = useState(enabled);
  const [kick, setKick] = useState(0);
  // The latest fetcher, without restarting the loop each render.
  const fetchNow = useEffectEvent((signal: AbortSignal) => fetcher(signal));

  // A new subject (deps) starts clean: no stale data from the previous one. Adjusted
  // while rendering (not in an effect), so the old subject's data never shows.
  const [subject, setSubject] = useState(deps);
  if (!sameDeps(subject, deps)) {
    setSubject(deps);
    setData(undefined);
    setError(undefined);
  }

  useEffect(() => {
    if (!enabled || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    let failures = 0; // consecutive failures of this loop: the backoff

    const tick = async () => {
      setLoading(true);
      try {
        const value = await fetchNow(controller.signal);
        if (!alive) return;
        failures = 0;
        setData(value);
        setError(undefined);
      } catch (err) {
        if (!alive || (err instanceof DOMException && err.name === "AbortError")) return;
        failures += 1;
        setError(err instanceof Error ? err : new Error(String(err)));
      } finally {
        if (alive) setLoading(false);
      }
      if (alive && interval > 0) {
        timer = setTimeout(tick, nextDelay(interval, failures));
      }
    };
    void tick();
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [enabled, visible, interval, kick, followEvents ? revision : 0, ...deps]); // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = useCallback(() => setKick((k) => k + 1), []);
  return { data, error, loading, refresh };
}
