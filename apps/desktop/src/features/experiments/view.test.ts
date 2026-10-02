import { describe, expect, it } from "vitest";
import { AgentdError, type CandidateVerdict, type Job, type ResearchItem, type ValidationReport } from "../../api";
import {
  canPropose,
  candidateOf,
  claimOutcome,
  convergenceSeries,
  decades,
  experimentsFor,
  headline,
  jobsMissingRuns,
  leadVerdict,
  memoryConflict,
  proposeBody,
  proposeOpen,
  shownExperiment,
  stepOf,
} from "./view";

const verdict = (evidence: CandidateVerdict["evidence"], failed: string[] = [], label = "vl"): CandidateVerdict => ({
  label,
  evidence,
  summary: "",
  checks: [
    { name: "accuracy", passed: true, detail: "" },
    ...failed.map((name) => ({ name, passed: false as const, detail: "" })),
  ],
});

describe("stepOf", () => {
  it("maps the main states", () => {
    expect(stepOf({ state: "awaiting_approval", evaluation: null })).toEqual({ current: 0, failed: false });
    expect(stepOf({ state: "executing", evaluation: null })).toEqual({ current: 1, failed: false });
    expect(stepOf({ state: "evaluating", evaluation: null })).toEqual({ current: 2, failed: false });
    expect(stepOf({ state: "reported", evaluation: null })).toEqual({ current: 3, failed: false });
  });
  it("places failures where they stopped", () => {
    expect(stepOf({ state: "rejected", evaluation: null })).toEqual({ current: 0, failed: true });
    expect(stepOf({ state: "cancelled", evaluation: null, jobs: [] })).toEqual({ current: 0, failed: true });
    expect(stepOf({ state: "failed", evaluation: null, jobs: [{ started_at: 1 } as Job] })).toEqual({ current: 1, failed: true });
    expect(stepOf({ state: "failed", evaluation: {} as ValidationReport })).toEqual({ current: 2, failed: true });
  });
});

describe("headline", () => {
  it("words the evidence without recomputing it", () => {
    expect(headline("yellow", verdict("yellow", ["claims"]))).toBe("Promising, with a failed claim");
    expect(headline("yellow", verdict("yellow", ["cost"]))).toBe("Promising, with caveats");
    expect(headline("green", verdict("green"))).toBe("Better, and the claims hold");
    expect(headline("red", verdict("red", ["integrity"]))).toBe("Integrity problem");
    expect(headline("red", verdict("red"))).toBe("Not better than the baseline");
    expect(headline("unknown")).toBe("Inconclusive");
  });
  it("picks the verdict matching the overall evidence", () => {
    const report = { evidence: "yellow", verdicts: [verdict("green", [], "a"), verdict("yellow", [], "b")], variants: [
      { label: "a", role: "candidate" },
      { label: "b", role: "candidate" },
    ] } as unknown as ValidationReport;
    const v = leadVerdict(report);
    expect(v?.label).toBe("b");
    expect(candidateOf(report, v)?.label).toBe("b");
  });
});

describe("claims", () => {
  it("reads holds", () => {
    expect(claimOutcome({ holds: true }).word).toBe("Passed");
    expect(claimOutcome({ holds: false }).word).toBe("Refuted");
    expect(claimOutcome({ holds: null }).word).toBe("Not tested");
  });
});

describe("convergence", () => {
  const job = (id: string, role: string, runs: unknown[] | undefined): Pick<Job, "id" | "label" | "role" | "results" | "state"> => ({
    id,
    label: id,
    role,
    state: "succeeded",
    results: runs ? ({ runs } as Job["results"]) : null,
  });
  it("builds sorted series, baseline first, skipping runs without errors", () => {
    const s = convergenceSeries([
      job("c", "candidate", [
        { nx: 64, dx: 1 / 64, l2_error: 1e-4 },
        { nx: 32, dx: 1 / 32, l2_error: 4e-4 },
      ]),
      job("b", "baseline", [{ nx: 32, dx: 1 / 32, l2_error: 1e-2 }, { nx: 64, dx: 1 / 64 }]),
      job("t", "candidate", [{ nx: 64, runtime_s: 1 }]),
    ]);
    expect(s.map((x) => x.label)).toEqual(["b", "c"]);
    expect(s[1].points.map((p) => p.nx)).toEqual([64, 32]);
    expect(s[0].points).toHaveLength(1);
  });
  it("lists jobs missing runs", () => {
    expect(jobsMissingRuns([job("a", "baseline", undefined), job("b", "baseline", [])])).toEqual(["a"]);
  });
  it("covers decades", () => {
    expect(decades(0.0011, 0.09)).toEqual([-3, -2, -1]);
    expect(decades(2e-5, 3e-5)).toEqual([-5, -4]);
  });
});

describe("experimentsFor", () => {
  it("filters and sorts newest first", () => {
    const list = [
      { id: "1", research_item_id: "p", created_at: 1 },
      { id: "2", research_item_id: "q", created_at: 2 },
      { id: "3", research_item_id: "p", created_at: 3 },
    ];
    expect(experimentsFor(list, "p").map((e) => e.id)).toEqual(["3", "1"]);
  });
  it("keeps a stable order within the same second", () => {
    const list = [
      { id: "exp_a", research_item_id: "p", created_at: 5 },
      { id: "exp_c", research_item_id: "p", created_at: 5 },
      { id: "exp_b", research_item_id: "p", created_at: 4 },
    ];
    expect(experimentsFor(list, "p").map((e) => e.id)).toEqual(["exp_c", "exp_a", "exp_b"]);
    expect(experimentsFor([...list].reverse(), "p").map((e) => e.id)).toEqual(["exp_c", "exp_a", "exp_b"]);
  });
});

describe("canPropose", () => {
  const ir = { scheme_ir: { name: "muscl" } } as unknown as ResearchItem["data"];
  it("takes carded and reported papers with a scheme", () => {
    expect(canPropose({ state: "carded", data: ir })).toBe(true);
    expect(canPropose({ state: "reported", data: ir })).toBe(true);
  });
  it("not while an experiment is under way, nor without a scheme", () => {
    expect(canPropose({ state: "experiment_planned", data: ir })).toBe(false);
    expect(canPropose({ state: "executing", data: ir })).toBe(false);
    expect(canPropose({ state: "reported", data: { scheme_ir: null } })).toBe(false);
  });
});

describe("shownExperiment", () => {
  const mine = [{ id: "new" }, { id: "old" }];
  it("shows the latest unless another one was picked", () => {
    expect(shownExperiment(mine, null)?.id).toBe("new");
    expect(shownExperiment(mine, "old")?.id).toBe("old");
  });
  it("falls back to the latest when the picked one is gone", () => {
    expect(shownExperiment(mine, "gone")?.id).toBe("new");
    expect(shownExperiment([], null)).toBeUndefined();
  });
});

describe("memoryConflict", () => {
  const err = (status: number, code: string | undefined, body: Record<string, unknown> = {}) =>
    new AgentdError("http", String(body.error ?? "nope"), { status, code, body: { ...body, code } });
  it("reads agentd's 409 already_tested verbatim, with the experiment it names", () => {
    const e = err(409, "already_tested", { error: "the same scheme was tested in exp_1 (yellow)", experiment_id: "exp_1" });
    expect(memoryConflict(e)).toEqual({
      code: "already_tested",
      message: "the same scheme was tested in exp_1 (yellow)",
      experimentId: "exp_1",
      researchItemId: undefined,
    });
  });
  it("reads already_planned with the paper it names", () => {
    const e = err(409, "already_planned", { error: "the same scheme is already planned (from ri_2)", research_item_id: "ri_2" });
    expect(memoryConflict(e)?.researchItemId).toBe("ri_2");
    expect(memoryConflict(e)?.message).toBe("the same scheme is already planned (from ri_2)");
  });
  it("leaves every other error alone", () => {
    expect(memoryConflict(err(409, "bad_state"))).toBeNull();
    expect(memoryConflict(err(422, "already_tested"))).toBeNull();
    expect(memoryConflict(new Error("already_tested"))).toBeNull();
    expect(memoryConflict(undefined)).toBeNull();
  });
});

describe("proposeBody", () => {
  const choice = { baseline: "muscl_vanleer", initial_condition: "gaussian", host_id: "auto", backend: "cpu" };
  it("sends what the form shows", () => {
    expect(proposeBody(choice)).toEqual(choice);
    expect(proposeBody(choice)).not.toHaveProperty("retest");
  });
  it("re-sends the current choice with retest, not the refused body", () => {
    // Refused with upwind; the user switched to van Leer, then "Propose anyway".
    expect(proposeBody(choice, true)).toEqual({ ...choice, retest: true });
  });
});

describe("proposeOpen", () => {
  const ir = { scheme_ir: { name: "muscl" } } as unknown as ResearchItem["data"];
  const reported = { id: "ri_1", state: "reported", data: ir } as const;
  it("stays open for the paper it was opened for", () => {
    expect(proposeOpen("ri_1", reported)).toBe(true);
    expect(proposeOpen(null, reported)).toBe(false);
  });
  it("closes on another paper, or once agentd no longer takes a propose", () => {
    expect(proposeOpen("ri_1", { ...reported, id: "ri_2" })).toBe(false);
    expect(proposeOpen("ri_1", { ...reported, state: "experiment_planned" })).toBe(false);
  });
});
