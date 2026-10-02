// Minimal typed client for the agentd HTTP API.
//
// The Tauri shell (command `agentd_connection`) says where agentd listens and which
// bearer token to send; this module only adds the header and maps failures to a
// small set of error kinds the UI can show. No business logic here.
// Never put the token into an error message or a log line.

import { invoke as tauriInvoke } from "@tauri-apps/api/core";
import type { AgentdConnection, Health, Host } from "./types";

export type AgentdErrorKind =
  /** agentd has not written its token file yet (never started with this data dir). */
  | "token_missing"
  /** The shell could not resolve the connection (bad env, unreadable file, not in Tauri). */
  | "config"
  /** Nothing answers at base_url: agentd is not running. */
  | "unreachable"
  /** agentd answered 401: the token doesn't match (different data dir?). */
  | "unauthorized"
  /** Any other non-2xx response. */
  | "http";

export class AgentdError extends Error {
  readonly kind: AgentdErrorKind;
  readonly status?: number;

  constructor(kind: AgentdErrorKind, message: string, status?: number) {
    super(message);
    this.name = "AgentdError";
    this.kind = kind;
    this.status = status;
  }
}

export type InvokeFn = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

export interface ClientDeps {
  invoke: InvokeFn;
  fetch: FetchFn;
}

function connectionError(err: unknown): AgentdError {
  if (err && typeof err === "object" && "code" in err && "message" in err) {
    const { code, message } = err as { code: unknown; message: unknown };
    return new AgentdError(code === "token_missing" ? "token_missing" : "config", String(message));
  }
  if (typeof err === "string") return new AgentdError("config", err);
  return new AgentdError(
    "config",
    "cannot ask the Newton shell for the agentd connection (is this running inside the Tauri app?)",
  );
}

async function errorText(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "error" in body) return String(body.error);
  } catch {
    // not JSON
  }
  return res.statusText || "request failed";
}

export function createAgentdClient(deps: Partial<ClientDeps> = {}) {
  const invoke: InvokeFn = deps.invoke ?? tauriInvoke;
  const doFetch: FetchFn = deps.fetch ?? ((input, init) => globalThis.fetch(input, init));

  /** Asks the shell each time, so the UI picks up agentd once it starts. */
  async function getConnection(): Promise<AgentdConnection> {
    try {
      return await invoke<AgentdConnection>("agentd_connection");
    } catch (err) {
      throw connectionError(err);
    }
  }

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const conn = await getConnection();
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${conn.token}`);
    headers.set("Accept", "application/json");

    let res: Response;
    try {
      res = await doFetch(`${conn.base_url}${path}`, { ...init, headers });
    } catch {
      throw new AgentdError("unreachable", `agentd is not running at ${conn.base_url}`);
    }
    if (res.status === 401) {
      throw new AgentdError(
        "unauthorized",
        `agentd at ${conn.base_url} rejected the token from ${conn.data_dir} ` +
          "(is agentd using a different NEWTON_DATA_DIR?)",
        401,
      );
    }
    if (!res.ok) {
      throw new AgentdError("http", `${path}: HTTP ${res.status}: ${await errorText(res)}`, res.status);
    }
    return (await res.json()) as T;
  }

  return {
    getConnection,
    request,
    health: () => request<Health>("/health"),
    hosts: () => request<Host[]>("/hosts"),
  };
}

export type AgentdClient = ReturnType<typeof createAgentdClient>;

/** The client wired to the real Tauri shell and window.fetch. */
export const agentd = createAgentdClient();
