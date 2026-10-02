import { describe, expect, it } from "vitest";
import type { CandidateVerdict, Job, ValidationReport } from "../../api";
import { candidateOf, claimOutcome, convergenceSeries, decades, experimentsFor, headline, jobsMissingRuns, leadVerdict, stepOf } from "./view";

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
});
