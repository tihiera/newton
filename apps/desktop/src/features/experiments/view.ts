// Pure presentation helpers for the experiment tab. They only arrange what agentd
// returned (states, verdicts, runs); they never judge a result.

import { AgentdError } from "../../api";
import type {
  Assumption,
  CandidateVerdict,
  Evidence,
  Experiment,
  ExperimentState,
  Job,
  ResearchItem,
  ValidationReport,
  VariantEvaluation,
} from "../../api";

export const STEPS = ["Plan", "Run", "Evaluate", "Reported"];

const STEP_OF: Partial<Record<ExperimentState, number>> = {
  awaiting_approval: 0,
  executing: 1,
  evaluating: 2,
  reported: 3,
};

export const TERMINAL_EXPERIMENT: ReadonlySet<string> = new Set(["reported", "failed", "rejected", "cancelled"]);
export const TERMINAL_JOB: ReadonlySet<string> = new Set(["succeeded", "failed", "timed_out", "cancelled", "rejected"]);

export function isTerminal(state: string): boolean {
  return TERMINAL_EXPERIMENT.has(state);
}

/** Where the stepper stands. A failed/cancelled experiment is shown failed at the
 *  step it stopped in: Plan when no job ever started, Evaluate when an evaluation
 *  exists, Run otherwise. Rejected stops at Plan. */
export function stepOf(exp: Pick<Experiment, "state" | "evaluation" | "jobs">): { current: number; failed: boolean } {
  const known = STEP_OF[exp.state];
  if (known !== undefined) return { current: known, failed: false };
  if (exp.state === "rejected") return { current: 0, failed: true };
  if (exp.evaluation) return { current: 2, failed: true };
  const started = (exp.jobs ?? []).some((j) => j.started_at);
  return { current: started ? 1 : 0, failed: true };
}

/** The verdict the headline speaks for: the one whose evidence is the experiment's
 *  overall evidence, else the first. */
export function leadVerdict(report: ValidationReport | null | undefined): CandidateVerdict | undefined {
  const verdicts = report?.verdicts ?? [];
  return verdicts.find((v) => v.evidence === report?.evidence) ?? verdicts[0];
}

/** Banner wording from the evidence colour and which checks failed (passed: false).
 *  Words only: the colour itself is agentd's. */
export function headline(evidence: Evidence | null | undefined, verdict?: CandidateVerdict): string {
  const failed = new Set((verdict?.checks ?? []).filter((c) => c.passed === false).map((c) => c.name));
  switch (evidence) {
    case "green":
      return "Better, and the claims hold";
    case "yellow":
      if (failed.has("claims")) return "Promising, with a failed claim";
      return "Promising, with caveats";
    case "red":
      if (failed.has("integrity")) return "Integrity problem";
      if (failed.has("completed")) return "Did not complete";
      if (failed.has("stable")) return "Unstable";
      return "Not better than the baseline";
    default:
      return "Inconclusive";
  }
}

export function baselineOf(report: ValidationReport | null | undefined): VariantEvaluation | undefined {
  return report?.variants.find((v) => v.role === "baseline");
}

/** The candidate a verdict is about (by label), else the first candidate. */
export function candidateOf(
  report: ValidationReport | null | undefined,
  verdict?: CandidateVerdict,
): VariantEvaluation | undefined {
  const cands = report?.variants.filter((v) => v.role === "candidate") ?? [];
  return cands.find((v) => v.label === verdict?.label) ?? cands[0];
}

const CLAIM_LABEL: Record<string, string> = {
  order: "Order claim",
  conservation: "Conservation",
  tvd: "TVD claim",
  max_cfl: "Max CFL claim",
  agrees_with_numpy: "Agrees with NumPy",
  deterministic: "Deterministic",
};

export function claimLabel(claim: string): string {
  return CLAIM_LABEL[claim] ?? claim.replace(/_/g, " ");
}

export function claimOutcome(a: Pick<Assumption, "holds">): {
  word: string;
  tone: "green" | "red" | "unknown";
  icon: string;
} {
  if (a.holds === true) return { word: "Passed", tone: "green", icon: "check" };
  if (a.holds === false) return { word: "Refuted", tone: "red", icon: "x" };
  return { word: "Not tested", tone: "unknown", icon: "info" };
}

// -- convergence chart ----------------------------------------------------------------

export interface SeriesPoint {
  dx: number;
  l2: number;
  nx: number;
}

export interface Series {
  label: string;
  role: string;
  jobId: string;
  points: SeriesPoint[];
}

const positive = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x) && x > 0;

/** One series per job with refinement runs (dx and l2_error both reported), points
 *  sorted by dx. Jobs without such runs (throughput mode, not finished) are left out. */
export function convergenceSeries(jobs: ReadonlyArray<Pick<Job, "id" | "label" | "role" | "results">>): Series[] {
  const out: Series[] = [];
  for (const j of jobs) {
    const runs = j.results?.runs ?? [];
    const points = runs
      .filter((r) => positive(r.dx) && positive(r.l2_error))
      .map((r) => ({ dx: r.dx as number, l2: r.l2_error as number, nx: r.nx }))
      .sort((a, b) => a.dx - b.dx);
    if (points.length) out.push({ label: j.label, role: j.role, jobId: j.id, points });
  }
  // Baseline first, so its colour (neutral) and legend slot are stable.
  return out.sort((a, b) => (a.role === "baseline" ? -1 : 0) - (b.role === "baseline" ? -1 : 0));
}

/** Integer decades covering [min, max] (inclusive, at least one decade wide). */
export function decades(min: number, max: number): number[] {
  const lo = Math.floor(Math.log10(min));
  let hi = Math.ceil(Math.log10(max));
  if (hi === lo) hi = lo + 1;
  const out: number[] = [];
  for (let e = lo; e <= hi; e++) out.push(e);
  return out;
}

/** Jobs whose results must still be fetched for the chart: finished successfully
 *  but the list view carried no runs. */
export function jobsMissingRuns(jobs: ReadonlyArray<Pick<Job, "id" | "state" | "results">>): string[] {
  return jobs.filter((j) => j.state === "succeeded" && !j.results?.runs).map((j) => j.id);
}

// -- a paper's experiments --------------------------------------------------------------

/** The paper's experiments, newest first (same second: the larger id first, so the
 *  order never flickers between reads). */
export function experimentsFor<T extends Pick<Experiment, "id" | "research_item_id" | "created_at">>(
  all: ReadonlyArray<T>,
  itemId: string,
): T[] {
  return all
    .filter((e) => e.research_item_id === itemId)
    .sort((a, b) => b.created_at - a.created_at || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));
}

/** Paper states agentd accepts a propose in: carded, or reported (a new experiment
 *  for a paper already tested). The paper also needs a scheme in Newton's IR. */
export function canPropose(item: Pick<ResearchItem, "state" | "data">): boolean {
  return !!item.data.scheme_ir && (item.state === "carded" || item.state === "reported");
}

/** The experiment the tab shows: the one picked from the list while it is still there,
 *  else the latest. `mine` is newest first (experimentsFor). */
export function shownExperiment<T extends Pick<Experiment, "id">>(
  mine: ReadonlyArray<T>,
  picked: string | null,
): T | undefined {
  return mine.find((e) => e.id === picked) ?? mine[0];
}

/** What the propose form shows: the baseline, the initial condition and where it runs. */
export interface ProposeChoice {
  baseline: string;
  initial_condition: string;
  host_id: string;
  backend: string;
}

/** The propose request for what the form shows right now. "Propose anyway" (`retest`)
 *  sends the current choice too, so a baseline changed after agentd's 409 is the one
 *  that runs, never the body that was refused. */
export function proposeBody(choice: ProposeChoice, retest = false): ProposeChoice & { retest?: boolean } {
  return retest ? { ...choice, retest: true } : { ...choice };
}

/** Whether the tab's "New experiment" form is open: it was opened for this paper
 *  (`openFor` is the paper's id) and agentd still takes a propose for it. */
export function proposeOpen(openFor: string | null, item: Pick<ResearchItem, "id" | "state" | "data">): boolean {
  return openFor === item.id && canPropose(item);
}

/** A propose refused by scientific memory (409 `already_tested` / `already_planned`):
 *  agentd's sentence verbatim and the ids it names. Anything else is an ordinary error. */
export interface MemoryConflict {
  code: "already_tested" | "already_planned";
  message: string;
  experimentId?: string;
  researchItemId?: string;
}

export function memoryConflict(err: unknown): MemoryConflict | null {
  if (!(err instanceof AgentdError) || err.status !== 409) return null;
  if (err.code !== "already_tested" && err.code !== "already_planned") return null;
  const id = (k: string) => (typeof err.body?.[k] === "string" ? (err.body[k] as string) : undefined);
  return {
    code: err.code,
    message: err.message,
    experimentId: id("experiment_id"),
    researchItemId: id("research_item_id"),
  };
}
