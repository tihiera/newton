import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("keeps underscores inside words", () => {
    const html = renderToStaticMarkup(createElement(Markdown, { text: "finite_volume_method_11014315 vs _upwind_" }));
    expect(html).toContain("finite_volume_method_11014315");
    expect(html).toContain("<em>upwind</em>");
  });
});
