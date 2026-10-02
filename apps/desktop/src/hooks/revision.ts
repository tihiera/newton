import { createContext } from "react";
import type { AgentdEvent } from "../api";

/** Bumped by EventsProvider whenever /events has something new: every polled view
 *  re-reads at once instead of waiting for its next tick. */
export const RevisionContext = createContext(0);

/** Re-read every polled view now: after a user action, since not every change (a
 *  profile edit, say) is an event. */
export const BumpContext = createContext<() => void>(() => {});

export interface EventsState {
  /** The most recent events (newest last), for timelines. */
  recent: AgentdEvent[];
  lastId: number;
}

export const EventsContext = createContext<EventsState>({ recent: [], lastId: 0 });
