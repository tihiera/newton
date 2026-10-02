import { describe, expect, it } from "vitest";
import type { AgentdErrorKind } from "../api";
import { connectionView, STARTING_RETRY_MS } from "./engineView";

describe("connectionView", () => {
  it("waits (logo and spinner) while Newton is not connected yet", () => {
    const waiting: AgentdErrorKind[] = ["token_missing", "unreachable", "engine_starting", "http"];
    for (const kind of waiting) {
      expect(connectionView(kind, { shell: true }).page).toBe("waiting");
      expect(connectionView(kind, { shell: false }).page).toBe("waiting");
    }
  });

  it("asks again every second while the engine starts, else follows the polling backoff", () => {
    expect(connectionView("engine_starting", { shell: true })).toEqual({ page: "waiting", retryMs: STARTING_RETRY_MS });
    expect(connectionView("unreachable", { shell: true })).toEqual({ page: "waiting", retryMs: null });
  });

  it("offers Restart when the engine failed inside the shell, Try again otherwise", () => {
    expect(connectionView("engine_failed", { shell: true })).toEqual({
      page: "failed",
      line: "Newton couldn't start.",
      action: "restart",
    });
    expect(connectionView("engine_failed", { shell: false })).toMatchObject({ page: "failed", action: "retry" });
  });

  it("shows one short line and Try again for a rejected token or a broken config", () => {
    for (const kind of ["unauthorized", "config"] as const) {
      const v = connectionView(kind, { shell: true });
      expect(v).toMatchObject({ page: "failed", action: "retry" });
      if (v.page === "failed") expect(v.line.length).toBeLessThan(50);
    }
  });
});
