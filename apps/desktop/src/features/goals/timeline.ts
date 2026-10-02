// The research timeline of one goal, built only from what agentd returned: the
// goal's events (created, poll), its papers, the experiments made from them, the
// pending approvals of those experiments, and the findings. Pure: no fetching, and
// nothing is judged here (states, evidence and sentences are agentd's).

import type { AgentdEvent, Approval, Experiment, Finding, PollSummary, ResearchItem } from "../../api";

export type PaperPhase = "reading" | "carded" | "dismissed" | "failed";

interface Base {
  key: string;
  ts: number;
}

export type TimelineEntry =
  | (Base & { kind: "created"; title: string | null })
  | (Base & { kind: "poll"; summary: PollSummary })
  | (Base & { kind: "paper"; phase: PaperPhase; item: ResearchItem })
  | (Base & { kind: "experiment"; experiment: Experiment; approvalId: string | null })
  | (Base & { kind: "result"; experiment: Experiment; finding: Finding | null })
  | (Base & { kind: "finding"; finding: Finding });

export interface TimelineInput {
  goalId: string;
  /** Events from any source (history + recent); filtered and de-duplicated here. */
  events: AgentdEvent[];
  papers?: ResearchItem[];
  experiments?: Experiment[];
  approvals?: Approval[];
  findings?: Finding[];
}

const READING = new Set(["discovered", "triaged", "extracting", "awaiting_approval"]);
const FINISHED = new Set(["reported", "failed", "rejected", "cancelled"]);

function isPollSummary(data: unknown): data is PollSummary {
  if (!data || typeof data !== "object") return false;
  const d = data as Record<string, unknown>;
  return typeof d.found === "number" && Array.isArray(d.proposed ?? []) && Array.isArray(d.skipped ?? []);
}

function normalizePoll(data: PollSummary): PollSummary {
  return { ...data, proposed: data.proposed ?? [], skipped: data.skipped ?? [] };
}

export function paperPhase(item: ResearchItem): PaperPhase | null {
  if (item.state === "dismissed") return "dismissed";
  if (item.state === "failed") return "failed";
  if (item.data.card) return "carded";
  if (READING.has(item.state)) return "reading";
  return null;
}

/** When the paper reached its phase: carding time from the extraction provenance. */
function paperTime(item: ResearchItem, phase: PaperPhase): number {
  if (phase === "reading") return item.created_at;
  if (phase === "carded") {
    const at = item.data.extraction?.at;
    return typeof at === "number" ? at : item.updated_at;
  }
  return item.updated_at;
}

export function buildTimeline(input: TimelineInput): TimelineEntry[] {
  const { goalId } = input;
  const entries: TimelineEntry[] = [];

  const seen = new Set<number>();
  for (const ev of input.events) {
    if (seen.has(ev.id) || ev.entity_type !== "goal" || ev.entity_id !== goalId) continue;
    seen.add(ev.id);
    if (ev.kind === "poll" && isPollSummary(ev.data)) {
      entries.push({ kind: "poll", key: `ev-${ev.id}`, ts: ev.ts, summary: normalizePoll(ev.data) });
    } else if (ev.kind === "created") {
      const title = typeof ev.data?.title === "string" ? ev.data.title : null;
      entries.push({ kind: "created", key: `ev-${ev.id}`, ts: ev.ts, title });
    }
  }

  const papers = (input.papers ?? []).filter((p) => p.goal_id === goalId);
  const paperIds = new Set(papers.map((p) => p.id));
  for (const item of papers) {
    const phase = paperPhase(item);
    if (phase) entries.push({ kind: "paper", key: `paper-${item.id}`, ts: paperTime(item, phase), phase, item });
  }

  const experiments = (input.experiments ?? []).filter(
    (e) => e.goal_id === goalId || (e.research_item_id !== null && paperIds.has(e.research_item_id)),
  );
  const pending = new Map<string, string>();
  for (const a of input.approvals ?? []) {
    if (a.status === "pending" && a.subject_type === "experiment") pending.set(a.subject_id, a.id);
  }
  const findings = (input.findings ?? []).filter(
    (f) => f.goal_id === goalId || (f.research_item_id !== null && paperIds.has(f.research_item_id)),
  );
  const findingByExperiment = new Map(findings.map((f) => [f.experiment_id, f]));
  const experimentIds = new Set(experiments.map((e) => e.id));

  for (const exp of experiments) {
    entries.push({
      kind: "experiment",
      key: `exp-${exp.id}`,
      ts: exp.created_at,
      experiment: exp,
      approvalId: pending.get(exp.id) ?? null,
    });
    if (FINISHED.has(exp.state)) {
      const finding = findingByExperiment.get(exp.id) ?? null;
      entries.push({
        kind: "result",
        key: `result-${exp.id}`,
        ts: finding?.created_at ?? exp.updated_at,
        experiment: exp,
        finding,
      });
    }
  }
  for (const f of findings) {
    if (!experimentIds.has(f.experiment_id)) {
      entries.push({ kind: "finding", key: `finding-${f.id}`, ts: f.created_at, finding: f });
    }
  }

  // Oldest first, like a conversation; ties keep a stable order by key.
  return entries.sort((a, b) => a.ts - b.ts || a.key.localeCompare(b.key));
}

/** "8 found · 3 relevant · 1 carded": the counts of a poll, as agentd reported them. */
export function pollLine(s: PollSummary): string {
  const parts = [`${s.found} found`];
  if (s.new !== s.found) parts.push(`${s.new} new`);
  if (s.relevant) parts.push(`${s.relevant} relevant`);
  if (s.dismissed) parts.push(`${s.dismissed} dismissed`);
  if (s.carded) parts.push(`${s.carded} carded`);
  if (s.proposed.length) parts.push(`${s.proposed.length} proposed`);
  if (s.skipped.length) parts.push(`${s.skipped.length} skipped`);
  return parts.join(" · ");
}

/** "upwind vs vanleer": the variants of an experiment, by label. */
export function variantsLine(exp: Experiment): string {
  const variants = exp.spec?.variants ?? [];
  const base = variants.filter((v) => v.role === "baseline").map((v) => v.label);
  const cand = variants.filter((v) => v.role === "candidate").map((v) => v.label);
  if (!base.length && !cand.length) return exp.title;
  if (!base.length) return cand.join(", ");
  return `${base.join(", ")} vs ${cand.join(", ")}`;
}
