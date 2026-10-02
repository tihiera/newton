import { describe, expect, it } from "vitest";
import { approvalExperimentId, destinationFacts, kindText, plain, shortCommit, shortHash, variantScheme } from "./text";

describe("approval text", () => {
  it("labels the approve button per kind", () => {
    expect(kindText("execute_experiment").approve).toBe("Approve & run");
    expect(kindText("start_service").approve).toBe("Approve & start");
    expect(kindText("publish_report").approve).toBe("Approve & publish");
    expect(kindText("something_else").approve).toBe("Approve");
  });

  it("shortens digests and commits", () => {
    expect(shortHash("81a9cafe00112233e4c2")).toBe("81a9…e4c2");
    expect(shortHash("sha256:81a9cafe00112233e4c2")).toBe("81a9…e4c2");
    expect(shortHash(null)).toBe("—");
    expect(shortCommit("7f2a9c1deadbeef")).toBe("7f2a9c1");
  });

  it("names the scheme of a variant", () => {
    expect(variantScheme({ params: { scheme: "upwind" } })).toEqual({ scheme: "upwind", digest: undefined });
    expect(
      variantScheme({ params: { scheme: "ir", scheme_ir: { name: "paper_vl" } }, scheme_ir_digest: "abc" }),
    ).toEqual({ scheme: "paper_vl", digest: "abc" });
  });

  it("reads publish destinations", () => {
    expect(destinationFacts("github", { kind: "gist" }).destination).toBe("GitHub Gist");
    expect(destinationFacts("github", { kind: "issue", repo: "o/r" })).toMatchObject({
      where: "o/r",
      whereLabel: "Repository",
    });
    expect(destinationFacts("notion", { parent_page_id: "a".repeat(32) })).toMatchObject({
      destination: "Notion",
      whereLabel: "Parent page",
    });
  });

  it("prints any value", () => {
    expect(plain(true)).toBe("Yes");
    expect(plain(null)).toBe("—");
    expect(plain({ a: 1 })).toContain('"a": 1');
  });
});

describe("approvalExperimentId", () => {
  it("names the experiment an execute approval is about, nothing else", () => {
    expect(approvalExperimentId({ subject_type: "experiment", subject_id: "exp-1" })).toBe("exp-1");
    expect(approvalExperimentId({ subject_type: "publication", subject_id: "pub-1" })).toBeNull();
    expect(approvalExperimentId({ subject_type: "experiment", subject_id: "" })).toBeNull();
  });
});
