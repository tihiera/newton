// How backend states read and look. Presentation only: the states themselves come
// from agentd; nothing here decides anything.

import type { Evidence } from "../api";

export type Tone = "green" | "yellow" | "red" | "lavender" | "blue" | "blush" | "gray";

const PAPER: Record<string, [string, Tone]> = {
  discovered: ["Discovered", "gray"],
  triaged: ["Relevant", "blue"],
  awaiting_approval: ["Awaiting approval", "yellow"],
  extracting: ["Extracting", "lavender"],
  carded: ["Carded", "green"],
  experiment_planned: ["Experiment planned", "yellow"],
  executing: ["Executing", "blue"],
  evaluating: ["Evaluating", "blue"],
  reported: ["Reported", "green"],
  dismissed: ["Dismissed", "gray"],
  failed: ["Failed", "red"],
};

const EXPERIMENT: Record<string, [string, Tone]> = {
  awaiting_approval: ["Awaiting approval", "yellow"],
  executing: ["Running", "blue"],
  evaluating: ["Evaluating", "lavender"],
  reported: ["Reported", "green"],
  failed: ["Failed", "red"],
  rejected: ["Rejected", "gray"],
  cancelled: ["Cancelled", "gray"],
};

const JOB: Record<string, [string, Tone]> = {
  pending_approval: ["Pending approval", "yellow"],
  queued: ["Queued", "gray"],
  submitting: ["Submitting", "lavender"],
  running: ["Running", "blue"],
  collecting: ["Collecting", "lavender"],
  succeeded: ["Succeeded", "green"],
  failed: ["Failed", "red"],
  timed_out: ["Timed out", "red"],
  cancelled: ["Cancelled", "gray"],
  rejected: ["Rejected", "gray"],
};

const SERVICE: Record<string, [string, Tone]> = {
  awaiting_approval: ["Awaiting approval", "yellow"],
  approved: ["Approved", "yellow"],
  starting: ["Starting", "lavender"],
  ready: ["Ready", "green"],
  draining: ["Draining", "yellow"],
  stopping: ["Stopping", "gray"],
  stopped: ["Stopped", "gray"],
  failed: ["Failed", "red"],
  lost: ["Lost", "red"],
  rejected: ["Rejected", "gray"],
  cancelled: ["Cancelled", "gray"],
};

const PUBLICATION: Record<string, [string, Tone]> = {
  awaiting_approval: ["Awaiting approval", "yellow"],
  approved: ["Approved", "yellow"],
  publishing: ["Publishing", "lavender"],
  published: ["Published", "green"],
  rejected: ["Rejected", "gray"],
  failed: ["Failed", "red"],
};

const GOAL: Record<string, [string, Tone]> = {
  active: ["Active", "green"],
  paused: ["Paused", "yellow"],
  archived: ["Archived", "gray"],
};

export const TABLES = { paper: PAPER, experiment: EXPERIMENT, job: JOB, service: SERVICE, publication: PUBLICATION, goal: GOAL };

export function stateLabel(kind: keyof typeof TABLES, state: string | null | undefined): [string, Tone] {
  if (!state) return ["—", "gray"];
  return TABLES[kind][state] ?? [state.replace(/_/g, " "), "gray"];
}

export const EVIDENCE: Record<Evidence, { label: string; tone: Tone }> = {
  green: { label: "Green evidence", tone: "green" },
  yellow: { label: "Yellow evidence", tone: "yellow" },
  red: { label: "Red evidence", tone: "red" },
  unknown: { label: "No evidence", tone: "gray" },
};

export function hostStatus(status: string): { label: string; dot: "ok" | "warn" | "bad" | "hollow" } {
  if (status === "online") return { label: "Online", dot: "ok" };
  if (status === "unknown") return { label: "Not checked", dot: "hollow" };
  if (status.startsWith("error:")) {
    const code = status.slice(6);
    const words: Record<string, string> = {
      hostkey_unknown: "Host key not trusted",
      unreachable: "Unreachable",
      deps_missing: "Setup needed",
    };
    return { label: words[code] ?? code.replace(/_/g, " "), dot: "bad" };
  }
  return { label: status, dot: "warn" };
}
