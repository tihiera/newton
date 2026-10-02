import { describe, expect, it } from "vitest";
import type { ResearchItem } from "../../api";
import {
  arxivYear,
  canPropose,
  correctionText,
  countByState,
  matchesQuery,
  paperMeta,
  paperSubline,
  paperYear,
  provenanceRows,
  timeMethodText,
} from "./format";

function item(over: Partial<ResearchItem> = {}, data: ResearchItem["data"] = {}): ResearchItem {
  return {
    id: "paper-1",
    goal_id: null,
    kind: "paper",
    title: "arXiv:2401.12345",
    source: "arxiv",
    external_id: "2401.12345",
    state: "discovered",
    created_at: 1,
    updated_at: 1,
    data,
    ...over,
  };
}

const paper = {
  arxiv_id: "2401.12345",
  title: "A TVD MUSCL scheme",
  abstract: "We study limiters.",
  authors: ["Ada Lovelace", "Alan Turing", "Emmy Noether", "Kurt Gödel"],
  published: "2023-11-02T00:00:00Z",
  categories: ["math.NA"],
  url: "https://arxiv.org/abs/2401.12345",
};

describe("years", () => {
  it("reads modern and legacy arXiv ids", () => {
    expect(arxivYear("2401.12345")).toBe("2024");
    expect(arxivYear("arXiv:0912.1234v2")).toBe("2009");
    expect(arxivYear("math/0501001")).toBe("2005");
    expect(arxivYear("hep-th/9901001")).toBe("1999");
    expect(arxivYear("nonsense")).toBeNull();
  });
  it("prefers the published date", () => {
    expect(paperYear(item({}, { paper }))).toBe("2023");
    expect(paperYear(item())).toBe("2024");
  });
});

describe("meta lines", () => {
  it("uses the first author, else a category, else arXiv", () => {
    expect(paperMeta(item({}, { paper }))).toBe("Ada Lovelace et al. · 2023");
    expect(paperMeta(item({}, { paper: { ...paper, authors: [] } }))).toBe("math.NA · 2023");
    expect(paperMeta(item())).toBe("arXiv · 2024");
  });
  it("builds the header sub line", () => {
    expect(paperSubline(item({}, { paper }))).toBe(
      "arXiv 2401.12345 · 2023 · Ada Lovelace, Alan Turing, Emmy Noether et al.",
    );
  });
});

describe("search", () => {
  const it1 = item({ title: "Flux limiters" }, {
    paper,
    card: {
      relevant: true,
      summary: "A van Leer limiter study",
      method: { name: "MUSCL", limiter: "van_leer", second_order_correction: true, time_integration: "rk2", order: 2, max_cfl: 0.8, tvd: true },
      claims: [],
      benchmarks: [],
    },
  });
  it("matches title, authors, summary and id, all words", () => {
    expect(matchesQuery(it1, "")).toBe(true);
    expect(matchesQuery(it1, "turing")).toBe(true);
    expect(matchesQuery(it1, "van leer")).toBe(true);
    expect(matchesQuery(it1, "2401.12345")).toBe(true);
    expect(matchesQuery(it1, "weno")).toBe(false);
  });
});

describe("counts", () => {
  it("counts by state in order of appearance", () => {
    expect(countByState([item({ state: "carded" }), item({ state: "failed" }), item({ state: "carded" })])).toEqual([
      ["carded", 2],
      ["failed", 1],
    ]);
  });
});

describe("scheme IR text", () => {
  it("prints the correction formula", () => {
    expect(correctionText(null)).toBe("none (first-order upwind)");
    expect(correctionText(0.5)).toBe("0.5");
    expect(correctionText({ op: "mul", args: [0.5, { op: "sub", args: [1, "c"] }] })).toBe("0.5 × (1 − c)");
  });
  it("prints the time method", () => {
    expect(timeMethodText({ method: "one_step" })).toBe("One step");
    expect(timeMethodText({ method: "rk", tableau: { a: [[1]], b: [0.5, 0.5] } })).toBe("Runge–Kutta, 2 stages");
  });
});

describe("provenance", () => {
  it("lists known keys first and skips nested values", () => {
    const rows = provenanceRows({ host: "spark", model: "llama3.2:3b", "request-id": "req-1", extra: { a: 1 }, zeta: "z" });
    expect(rows.map((r) => r.key)).toEqual(["model", "host", "request-id", "zeta"]);
    expect(rows[2].label).toBe("Router request");
  });
});

describe("propose gate", () => {
  it("needs a mapped scheme on a carded paper", () => {
    const ir = { name: "x", flux: { limiter: "none", correction: null }, time: { method: "one_step" as const }, claims: { order: 1, max_cfl: 1, tvd: false } };
    expect(canPropose(item({ state: "carded" }, { scheme_ir: ir }))).toBe(true);
    expect(canPropose(item({ state: "experiment_planned" }, { scheme_ir: ir }))).toBe(false);
    expect(canPropose(item({ state: "carded" }, { scheme_ir: null }))).toBe(false);
  });
});
