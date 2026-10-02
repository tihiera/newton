import { describe, expect, it } from "vitest";
import { AgentdError } from "../api";
import { formError } from "./ui";

const e422 = (fields: Record<string, string>) => new AgentdError("http", "invalid", { status: 422, fields });

describe("formError", () => {
  it("hides a 422 whose fields the form already shows", () => {
    expect(formError(e422({ ssh_target: "bad" }), ["ssh_target", "name"])).toBeUndefined();
  });

  it("shows a 422 on a key no input displays", () => {
    const err = e422({ ssh_user: "contains characters that are not allowed" });
    expect(formError(err, ["ssh_target", "name"])).toBe(err);
  });

  it("shows errors without fields, and nothing without an error", () => {
    const err = new AgentdError("http", "host not found", { status: 404 });
    expect(formError(err, ["name"])).toBe(err);
    expect(formError(undefined, ["name"])).toBeUndefined();
  });
});
