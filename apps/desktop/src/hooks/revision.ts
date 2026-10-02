import { createContext } from "react";
import type { AgentdEvent } from "../api";

/** Bumped by EventsProvider whenever /events has something new: every polled view
 *  re-reads at once instead of waiting for its next tick. */
export const RevisionContext = createContext(0);

export interface EventsState {
  /** The most recent events (newest last), for timelines. */
  recent: AgentdEvent[];
  lastId: number;
}

export const EventsContext = createContext<EventsState>({ recent: [], lastId: 0 });
