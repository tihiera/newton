// What the window shows while it can't show Newton yet. Waiting (not connected yet,
// the engine starting, its health check failing): the logo and a spinner, no text.
// A failure that waiting can't fix (the engine failed, the token was rejected, the
// shell couldn't say where the engine listens): one short line and one button.

import type { AgentdErrorKind } from "../api";

export type ConnectionView =
  | {
      page: "waiting";
      /** Try again this soon (ms) instead of the polling backoff: while the engine starts. */
      retryMs: number | null;
    }
  | {
      page: "failed";
      line: string;
      /** "restart": Restart (the Tauri shell starts its engine again); "retry": Try again. */
      action: "restart" | "retry";
    };

/** How often the window asks again while the shell says the engine is starting. */
export const STARTING_RETRY_MS = 1000;

const FAILED_LINES: Partial<Record<AgentdErrorKind, string>> = {
  engine_failed: "Newton couldn't start.",
  unauthorized: "Newton couldn't connect to its engine.",
  config: "Newton couldn't find its engine.",
};

export function connectionView(kind: AgentdErrorKind, env: { shell: boolean }): ConnectionView {
  const line = FAILED_LINES[kind];
  if (line === undefined) {
    return { page: "waiting", retryMs: kind === "engine_starting" ? STARTING_RETRY_MS : null };
  }
  return { page: "failed", line, action: kind === "engine_failed" && env.shell ? "restart" : "retry" };
}
