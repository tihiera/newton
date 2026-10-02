// Tails a job's stdout or stderr by offset: each read returns {data, next_offset, size};
// the next read starts at next_offset. Only while `enabled` (the log is expanded and
// visible) and only until the job is finished and the log is caught up.

import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { nextDelay } from "./usePolling";
import { useVisible } from "./useVisibility";

const EVERY = 1500;
const MAX_CHARS = 400_000; // keep the tail of very long logs

export function useJobLogs(jobId: string, stream: "stdout" | "stderr", enabled: boolean, finished: boolean) {
  const visible = useVisible();
  // Each log (job + stream) keeps its own text and read position, so switching between
  // stdout and stderr resumes each where it was: never re-read, never shown twice.
  const key = `${jobId}:${stream}`;
  const [texts, setTexts] = useState<Record<string, string>>({});
  const offsets = useRef(new Map<string, number>());
  const failures = useRef(0);

  useEffect(() => {
    if (!enabled || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    const tick = async () => {
      let caughtUp = false;
      try {
        const chunk = await api.jobs.logs(jobId, stream, offsets.current.get(key) ?? 0, controller.signal);
        if (!alive) return;
        failures.current = 0;
        if (chunk.data) setTexts((t) => ({ ...t, [key]: ((t[key] ?? "") + chunk.data).slice(-MAX_CHARS) }));
        offsets.current.set(key, chunk.next_offset);
        caughtUp = chunk.size !== undefined ? chunk.next_offset >= chunk.size : !chunk.data;
      } catch (err) {
        if (!alive || (err instanceof DOMException && err.name === "AbortError")) return;
        failures.current += 1;
      }
      if (!alive || (finished && caughtUp)) return;
      timer = setTimeout(tick, caughtUp ? nextDelay(EVERY, failures.current) : 50);
    };
    void tick();
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobId, stream, key, enabled, visible, finished]);

  return texts[key] ?? "";
}
