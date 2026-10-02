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
  const [text, setText] = useState("");
  const offset = useRef(0);
  const failures = useRef(0);

  useEffect(() => {
    setText("");
    offset.current = 0;
  }, [jobId, stream]);

  useEffect(() => {
    if (!enabled || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    const tick = async () => {
      let caughtUp = false;
      try {
        const chunk = await api.jobs.logs(jobId, stream, offset.current, controller.signal);
        if (!alive) return;
        failures.current = 0;
        if (chunk.data) setText((t) => (t + chunk.data).slice(-MAX_CHARS));
        offset.current = chunk.next_offset;
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
  }, [jobId, stream, enabled, visible, finished]);

  return text;
}
