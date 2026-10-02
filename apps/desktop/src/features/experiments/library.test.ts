import { describe, expect, it } from "vitest";
import type { LibraryScheme } from "../../api";
import {
  canPickCandidate,
  DEFAULT_LIBRARY_CHOICE,
  libraryBody,
  paperlessExperiments,
  schemeClaims,
  toggleCandidate,
  withBaseline,
} from "./library";

describe("libraryBody", () => {
  it("sends the choice, with goal_id only from a goal", () => {
    const choice = { ...DEFAULT_LIBRARY_CHOICE, candidates: ["muscl_vanleer"] };
    expect(libraryBody(choice)).toEqual({
      candidates: ["muscl_vanleer"],
      baseline: "upwind",
      initial_condition: "sine",
      host_id: "auto",
      backend: "auto",
    });
    expect(libraryBody(choice, "goal-1").goal_id).toBe("goal-1");
    expect("goal_id" in libraryBody(choice, null)).toBe(false);
  });

  it("does not share the candidates array", () => {
    const choice = { ...DEFAULT_LIBRARY_CHOICE, candidates: ["a"] };
    const body = libraryBody(choice);
    body.candidates.push("b");
    expect(choice.candidates).toEqual(["a"]);
  });
});

describe("toggleCandidate", () => {
  const order = ["upwind", "lax_wendroff", "muscl_minmod", "muscl_vanleer"];
  it("adds in the library's order and removes again", () => {
    let picked = toggleCandidate([], "muscl_vanleer", order);
    picked = toggleCandidate(picked, "lax_wendroff", order);
    expect(picked).toEqual(["lax_wendroff", "muscl_vanleer"]);
    expect(toggleCandidate(picked, "lax_wendroff", order)).toEqual(["muscl_vanleer"]);
  });
  it("keeps names the library doesn't list (last)", () => {
    expect(toggleCandidate(["zzz"], "upwind", order)).toEqual(["upwind", "zzz"]);
  });
});

describe("withBaseline", () => {
  it("drops a candidate that becomes the baseline and keeps the rest", () => {
    const choice = { ...DEFAULT_LIBRARY_CHOICE, candidates: ["lax_wendroff", "muscl_vanleer"] };
    const next = withBaseline(choice, "lax_wendroff");
    expect(next.baseline).toBe("lax_wendroff");
    expect(next.candidates).toEqual(["muscl_vanleer"]);
    expect(choice.candidates).toEqual(["lax_wendroff", "muscl_vanleer"]);
    expect(withBaseline(choice, "upwind").candidates).toEqual(["lax_wendroff", "muscl_vanleer"]);
  });
  it("never lets the baseline be picked as a candidate", () => {
    expect(canPickCandidate(DEFAULT_LIBRARY_CHOICE, "upwind")).toBe(false);
    expect(canPickCandidate(DEFAULT_LIBRARY_CHOICE, "lax_wendroff")).toBe(true);
    const body = libraryBody(withBaseline({ ...DEFAULT_LIBRARY_CHOICE, candidates: ["muscl_minmod"] }, "muscl_minmod"));
    expect(body.candidates).not.toContain(body.baseline);
  });
});

describe("schemeClaims", () => {
  const scheme = (claims: LibraryScheme["document"]["claims"]): Pick<LibraryScheme, "document"> => ({
    document: { name: "x", flux: { limiter: "vanleer", correction: null }, time: { method: "one_step" }, claims },
  });
  it("reads the IR's claims", () => {
    expect(schemeClaims(scheme({ order: 2, max_cfl: 1, tvd: true }))).toBe("order 2 · TVD · CFL ≤ 1");
    expect(schemeClaims(scheme({ order: 1, max_cfl: 0.5, tvd: false }))).toBe("order 1 · CFL ≤ 0.5");
  });
});

describe("paperlessExperiments", () => {
  const exp = (id: string, research_item_id: string | null, goal_id: string | null, created_at: number) => ({
    id,
    research_item_id,
    goal_id,
    created_at,
  });
  const all = [exp("a", null, null, 1), exp("b", "paper-1", null, 5), exp("c", null, "g1", 3), exp("d", null, null, 3)];
  it("keeps experiments without a paper, newest first", () => {
    expect(paperlessExperiments(all).map((e) => e.id)).toEqual(["d", "c", "a"]);
  });
  it("narrows to a goal when given one (null: the library's own)", () => {
    expect(paperlessExperiments(all, "g1").map((e) => e.id)).toEqual(["c"]);
    expect(paperlessExperiments(all, null).map((e) => e.id)).toEqual(["d", "a"]);
  });
});
