import { describe, expect, it } from "vitest";
import type { Goal, ResearchItem } from "../../api";
import { actionTarget, isFirstRun, readinessLine, stateLook } from "./readiness";

describe("stateLook", () => {
  it("has an icon per state, and treats an unknown state as a warning", () => {
    expect(stateLook("ok").icon).toBe("check");
    expect(stateLook("missing").icon).toBe("x");
    expect(stateLook("warn").icon).toBe("alert");
    expect(stateLook("later").label).toBe("Needs attention");
  });
});

describe("actionTarget", () => {
  it("opens the drawers", () => {
    expect(actionTarget("open_models")).toEqual({ kind: "models" });
    expect(actionTarget("open_compute")).toEqual({ kind: "compute" });
    expect(actionTarget("open_settings")).toEqual({ kind: "settings" });
  });
  it("opens the built-in experiment dialog for the goal", () => {
    expect(actionTarget("new_experiment", "g1")).toEqual({ kind: "new-experiment", goalId: "g1" });
    expect(actionTarget("new_experiment")).toEqual({ kind: "new-experiment", goalId: null });
  });
  it("ignores kinds it doesn't know", () => {
    expect(actionTarget("open_teleporter")).toBeNull();
  });
});

describe("readinessLine / isFirstRun", () => {
  it("counts the ready items", () => {
    expect(readinessLine([{ state: "ok" }, { state: "warn" }, { state: "ok" }])).toBe("2 of 3 ready");
  });
  it("is the first run only when goals and papers are read and both empty", () => {
    expect(isFirstRun([], [])).toBe(true);
    expect(isFirstRun(undefined, [])).toBe(false);
    expect(isFirstRun([], [{} as ResearchItem])).toBe(false);
    expect(isFirstRun([{} as Goal], [])).toBe(false);
  });
});
