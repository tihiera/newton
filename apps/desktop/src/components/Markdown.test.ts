import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

const html = (text: string) => renderToStaticMarkup(createElement(Markdown, { text }));

describe("Markdown", () => {
  it("keeps underscores inside words", () => {
    const out = html("finite_volume_method_11014315 vs _upwind_");
    expect(out).toContain("finite_volume_method_11014315");
    expect(out).toContain("<em>upwind</em>");
  });

  it("finds emphasis at the start, after punctuation and after a word with underscores", () => {
    expect(html("_a_ b")).toContain("<em>a</em>");
    expect(html("(_b_)")).toContain("<em>b</em>");
    expect(html("x_y _z_")).toContain("x_y <em>z</em>");
    expect(html("snake_case_name")).not.toContain("<em>");
  });

  it("uses no regex lookbehind (a syntax error on macOS 13's WebKit)", () => {
    expect(readFileSync(new URL("./Markdown.tsx", import.meta.url), "utf8")).not.toMatch(/\(\?<[=!]/);
  });
});
