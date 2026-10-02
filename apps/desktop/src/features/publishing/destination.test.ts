import { describe, expect, it } from "vitest";
import { connectorOf, missingInput, publishRequest } from "./destination";

describe("publishRequest", () => {
  it("maps each choice to agentd's shape", () => {
    expect(publishRequest("gist", "x", "y")).toEqual({ target: "github", destination: {} });
    expect(publishRequest("issue", " o/r ", "")).toEqual({ target: "github", destination: { kind: "issue", repo: "o/r" } });
    expect(publishRequest("notion", "", " abc ")).toEqual({ target: "notion", destination: { parent_page_id: "abc" } });
  });
  it("knows what is missing", () => {
    expect(missingInput("gist", "", "")).toBeNull();
    expect(missingInput("issue", "", "")).toBe("repo");
    expect(missingInput("notion", "", "")).toBe("parent_page_id");
    expect(connectorOf("issue")).toBe("github");
  });
});
