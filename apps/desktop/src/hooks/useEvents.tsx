// The global change signal: /events?after=<last id>, every 2 s while visible. Any new
// event bumps the revision, so every polled view re-reads at once.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type AgentdEvent } from "../api";
import { nextDelay } from "./usePolling";
import { EventsContext, RevisionContext, type EventsState } from "./revision";
import { useVisible } from "./useVisibility";

const EVERY = 2000;
const KEEP = 400;

export function EventsProvider({ children }: { children: ReactNode }) {
  const visible = useVisible();
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<EventsState>({ recent: [], lastId: 0 });
  const lastId = useRef<number | null>(null);
  const failures = useRef(0);

  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    const tick = async () => {
      try {
        let fresh: AgentdEvent[];
        if (lastId.current === null) {
          // First read: page forward (oldest first) until caught up with "now".
          fresh = [];
          let after = 0;
          for (;;) {
            const page = await api.events(after, { limit: 1000 }, controller.signal);
            fresh = [...fresh, ...page].slice(-KEEP);
            if (page.length < 1000) break;
            after = page[page.length - 1].id;
          }
        } else {
          fresh = await api.events(lastId.current, { limit: 500 }, controller.signal);
        }
        if (!alive) return;
        failures.current = 0;
        if (fresh.length) {
          const first = lastId.current === null;
          lastId.current = fresh[fresh.length - 1].id;
          setState((s) => ({
            recent: [...s.recent, ...fresh].slice(-KEEP),
            lastId: lastId.current ?? 0,
          }));
          if (!first) setRevision((r) => r + 1);
        } else if (lastId.current === null) {
          lastId.current = 0;
        }
      } catch (err) {
        if (!alive || (err instanceof DOMException && err.name === "AbortError")) return;
        failures.current += 1;
      }
      if (alive) timer = setTimeout(tick, nextDelay(EVERY, failures.current));
    };
    void tick();
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [visible]);

  return (
    <RevisionContext.Provider value={revision}>
      <EventsContext.Provider value={state}>{children}</EventsContext.Provider>
    </RevisionContext.Provider>
  );
}
