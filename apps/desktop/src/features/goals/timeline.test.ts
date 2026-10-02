import { describe, expect, it } from "vitest";
import type { AgentdEvent, Approval, Experiment, Finding, PollSummary, ResearchItem } from "../../api";
import { buildTimeline, paperPhase, pollLine, variantsLine } from "./timeline";

const G = "goal-1";

function paper(id: string, state: ResearchItem["state"], over: Partial<ResearchItem> = {}, data: ResearchItem["data"] = {}): ResearchItem {
  return {
    id,
    goal_id: G,
    kind: "paper",
    title: id,
    source: "arxiv",
    external_id: "2401.00001",
    state,
    created_at: 10,
    updated_at: 20,
    data,
    ...over,
  };
}

const card = {
  relevant: true,
  summary: "s",
  method: { name: "MUSCL", limiter: "van_leer", second_order_correction: true, time_integration: "rk2", order: 2, max_cfl: 0.8, tvd: true },
  claims: [],
  benchmarks: [],
};

function experiment(id: string, over: Partial<Experiment> = {}): Experiment {
  return {
    id,
    goal_id: null,
    research_item_id: "paper-c",
    host_id: "spark",
    title: "MUSCL vs upwind",
    spec: {
      title: "MUSCL vs upwind",
      benchmark: "linear_advection_1d",
      objective: "accuracy",
      host_id: "auto",
      backend: "auto",
      timeout_seconds: 600,
      variants: [
        { role: "baseline", label: "upwind", params: { scheme: "upwind" } },
        { role: "candidate", label: "vanleer", params: { scheme: "ir" } },
      ],
    },
    state: "awaiting_approval",
    evaluation: null,
    evidence: null,
    report_path: null,
    error: null,
    created_at: 40,
    updated_at: 40,
    ...over,
  };
}

const poll: PollSummary = { goal_id: G, found: 8, new: 8, relevant: 3, dismissed: 5, carded: 1, proposed: [], skipped: [] };

function ev(id: number, kind: string, data: Record<string, unknown>, ts = id, entity_id = G): AgentdEvent {
  return { id, ts, entity_type: "goal", entity_id, kind, data };
}

describe("buildTimeline", () => {
  it("keeps only this goal's created/poll events, once each", () => {
    const events = [
      ev(1, "created", { title: "Advection" }),
      ev(2, "poll", { ...poll }),
      ev(2, "poll", { ...poll }),
      ev(3, "updated", { fields: ["status"] }),
      ev(4, "poll", { ...poll }, 4, "goal-other"),
      { ...ev(5, "poll", { ...poll }), entity_type: "research_item" },
    ];
    const t = buildTimeline({ goalId: G, events });
    expect(t.map((e) => e.kind)).toEqual(["created", "poll"]);
  });

  it("ignores malformed poll data", () => {
    expect(buildTimeline({ goalId: G, events: [ev(1, "poll", { nope: true })] })).toEqual([]);
  });

  it("turns papers into phases with their times", () => {
    const papers = [
      paper("paper-r", "extracting", { created_at: 11 }),
      paper("paper-c", "experiment_planned", {}, { card, extraction: { at: 30 } }),
      paper("paper-d", "dismissed", { updated_at: 25 }),
      paper("paper-f", "failed", { updated_at: 26 }),
      paper("paper-x", "carded", { goal_id: "goal-other" }, { card }),
    ];
    const t = buildTimeline({ goalId: G, events: [], papers });
    expect(t.map((e) => (e.kind === "paper" ? `${e.item.id}:${e.phase}@${e.ts}` : e.kind))).toEqual([
      "paper-r:reading@11",
      "paper-d:dismissed@25",
      "paper-f:failed@26",
      "paper-c:carded@30",
    ]);
  });

  it("attaches pending approvals to the goal's experiments and adds results", () => {
    const papers = [paper("paper-c", "experiment_planned", {}, { card })];
    const approvals: Approval[] = [
      { id: "req-1", kind: "execute_experiment", subject_type: "experiment", subject_id: "exp-1", title: "Run", details: {}, status: "pending", created_at: 41 },
      { id: "req-2", kind: "execute_experiment", subject_type: "experiment", subject_id: "exp-9", title: "Run", details: {}, status: "pending", created_at: 41 },
    ];
    const finding: Finding = {
      id: "finding-1", goal_id: G, research_item_id: "paper-c", experiment_id: "exp-2", scheme_name: "vanleer",
      scheme_digest: null, evidence: "yellow", claims: [{ claim: "tvd", claimed: true, holds: false }], summary: "Better, TVD refuted", created_at: 90,
    };
    const orphan: Finding = { ...finding, id: "finding-2", experiment_id: "exp-gone", created_at: 95 };
    const experiments = [
      experiment("exp-1"),
      experiment("exp-2", { state: "reported", evidence: "yellow", created_at: 50, updated_at: 80 }),
      experiment("exp-3", { research_item_id: "paper-elsewhere" }),
      experiment("exp-4", { research_item_id: null, goal_id: G, state: "failed", error: "boom", created_at: 60, updated_at: 70 }),
    ];
    const t = buildTimeline({ goalId: G, events: [], papers, experiments, approvals, findings: [finding, orphan] });
    const keys = t.map((e) => e.key);
    expect(keys).not.toContain("exp-exp-3");
    const e1 = t.find((e) => e.key === "exp-exp-1");
    expect(e1?.kind === "experiment" && e1.approvalId).toBe("req-1");
    const r2 = t.find((e) => e.key === "result-exp-2");
    expect(r2?.kind === "result" && r2.finding?.id).toBe("finding-1");
    expect(r2?.ts).toBe(90);
    const r4 = t.find((e) => e.key === "result-exp-4");
    expect(r4?.kind === "result" && r4.finding).toBeNull();
    expect(keys.at(-1)).toBe("finding-finding-2");
    // sorted oldest first
    const ts = t.map((e) => e.ts);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
  });
});

describe("helpers", () => {
  it("phases", () => {
    expect(paperPhase(paper("p", "discovered"))).toBe("reading");
    expect(paperPhase(paper("p", "reported", {}, { card }))).toBe("carded");
    expect(paperPhase(paper("p", "reported"))).toBeNull();
  });
  it("poll line", () => {
    expect(pollLine(poll)).toBe("8 found · 3 relevant · 5 dismissed · 1 carded");
    expect(pollLine({ ...poll, found: 25, new: 2, relevant: 0, dismissed: 0, carded: 0, proposed: ["exp-1"], skipped: [{ item: "p", why: "x" }] })).toBe(
      "25 found · 2 new · 1 proposed · 1 skipped",
    );
  });
  it("variants line", () => {
    expect(variantsLine(experiment("e"))).toBe("upwind vs vanleer");
  });
});
