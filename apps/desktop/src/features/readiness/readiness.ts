// How GET /readiness reads: each item's state as an icon and tone, what its action
// button opens, and when the checklist is the first-run card. Presentation only:
// states, titles and sentences are agentd's.

import type { Goal, ReadinessAction, ReadinessItem, ReadinessState, ResearchItem } from "../../api";
import type { Overlay } from "../../app/navigation";

export interface StateLook {
  icon: string;
  tile: "mint" | "" | "blush";
  label: string;
}

const LOOKS: Record<ReadinessState, StateLook> = {
  ok: { icon: "check", tile: "mint", label: "Ready" },
  warn: { icon: "alert", tile: "", label: "Needs attention" },
  missing: { icon: "x", tile: "blush", label: "Missing" },
};

export function stateLook(state: ReadinessState | string): StateLook {
  return LOOKS[state as ReadinessState] ?? LOOKS.warn;
}

/** The drawer or dialog an action button opens. */
export function actionTarget(
  kind: ReadinessAction | string,
  goalId: string | null = null,
): NonNullable<Overlay> | null {
  switch (kind) {
    case "open_models":
      return { kind: "models" };
    case "open_compute":
      return { kind: "compute" };
    case "open_settings":
      return { kind: "settings" };
    case "new_experiment":
      return { kind: "new-experiment", goalId };
    default:
      return null;
  }
}

/** "5 of 7 ready". */
export function readinessLine(items: ReadonlyArray<Pick<ReadinessItem, "state">>): string {
  const ok = items.filter((i) => i.state === "ok").length;
  return `${ok} of ${items.length} ready`;
}

/** No research yet: no goals and no papers (both read). */
export function isFirstRun(goals: Goal[] | undefined, papers: ResearchItem[] | undefined): boolean {
  return goals !== undefined && papers !== undefined && goals.length === 0 && papers.length === 0;
}
