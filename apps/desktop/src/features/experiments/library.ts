// "New experiment from built-in schemes": the request the dialog sends and how the
// library and paperless experiments read. Presentation only: agentd checks the names,
// builds the study and asks for approval.

import type { Experiment, LibraryExperimentCreate, LibraryScheme } from "../../api";

export interface LibraryChoice {
  candidates: string[];
  baseline: string;
  initial_condition: string;
  host_id: string;
  backend: string;
}

export const DEFAULT_LIBRARY_CHOICE: LibraryChoice = {
  candidates: [],
  baseline: "upwind",
  initial_condition: "sine",
  host_id: "auto",
  backend: "auto",
};

/** The POST /experiments/library body for what the dialog shows; goal_id only when
 *  opened from a research goal. */
export function libraryBody(choice: LibraryChoice, goalId?: string | null): LibraryExperimentCreate {
  return {
    ...(goalId ? { goal_id: goalId } : {}),
    candidates: [...choice.candidates],
    baseline: choice.baseline,
    initial_condition: choice.initial_condition,
    host_id: choice.host_id,
    backend: choice.backend,
  };
}

/** Adds or removes one candidate, keeping the library's order. */
export function toggleCandidate(picked: string[], name: string, order: string[]): string[] {
  const next = picked.includes(name) ? picked.filter((n) => n !== name) : [...picked, name];
  const at = (n: string) => {
    const i = order.indexOf(n);
    return i < 0 ? order.length : i;
  };
  return next.sort((a, b) => at(a) - at(b));
}

/** Picks the baseline. The baseline can't also be a candidate (agentd rejects that),
 *  so a scheme picked as a candidate is dropped when it becomes the baseline. */
export function withBaseline(choice: LibraryChoice, baseline: string): LibraryChoice {
  return { ...choice, baseline, candidates: choice.candidates.filter((n) => n !== baseline) };
}

/** Whether a library scheme can be picked as a candidate: not while it is the baseline. */
export function canPickCandidate(choice: Pick<LibraryChoice, "baseline">, name: string): boolean {
  return name !== choice.baseline;
}

/** "order 2 · TVD · CFL ≤ 1": what the scheme's IR claims, as a line. */
export function schemeClaims(s: Pick<LibraryScheme, "document">): string {
  const c = s.document?.claims;
  if (!c) return "";
  const parts = [`order ${c.order}`];
  if (c.tvd) parts.push("TVD");
  if (typeof c.max_cfl === "number") parts.push(`CFL ≤ ${c.max_cfl}`);
  return parts.join(" · ");
}

/** Experiments not made from a paper (built-in schemes), newest first; with a goal,
 *  only that goal's. */
export function paperlessExperiments<T extends Pick<Experiment, "id" | "research_item_id" | "goal_id" | "created_at">>(
  all: ReadonlyArray<T>,
  goalId?: string | null,
): T[] {
  return all
    .filter((e) => !e.research_item_id && (goalId === undefined || e.goal_id === goalId))
    .sort((a, b) => b.created_at - a.created_at || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));
}
