// Typed client for the agentd HTTP API.
//
// The Tauri shell (command `agentd_connection`) says where agentd listens and which
// bearer token to send; this module adds the header, sends JSON, and maps failures to
// AgentdError (errors.ts). No business logic here. Never put the token into an error
// message or a log line.

import { invoke as tauriInvoke } from "@tauri-apps/api/core";
import { AgentdError, parseErrorBody } from "./errors";
import type { AgentdConnection, Health, Host } from "./types";

export { AgentdError, type AgentdErrorKind } from "./errors";

export type InvokeFn = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

export interface ClientDeps {
  invoke: InvokeFn;
  fetch: FetchFn;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** Sent as JSON. */
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
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

/** `pnpm dev:web` in a plain browser has no Tauri shell: in development only, the
 *  connection may come from VITE_AGENTD_URL / VITE_AGENTD_TOKEN (never in a build). */
function devConnection(): AgentdConnection | null {
  if (!import.meta.env?.DEV) return null;
  const url = import.meta.env.VITE_AGENTD_URL as string | undefined;
  const token = import.meta.env.VITE_AGENTD_TOKEN as string | undefined;
  return url && token ? { base_url: url, token, data_dir: "(dev)" } : null;
}

function withQuery(path: string, query?: RequestOptions["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}${path.includes("?") ? "&" : "?"}${qs}` : path;
}

export function createAgentdClient(deps: Partial<ClientDeps> = {}) {
  const invoke: InvokeFn = deps.invoke ?? tauriInvoke;
  const doFetch: FetchFn = deps.fetch ?? ((input, init) => globalThis.fetch(input, init));
  const useDev = !deps.invoke;

  /** Asks the shell each time, so the UI picks up agentd once it starts. */
  async function getConnection(): Promise<AgentdConnection> {
    try {
      return await invoke<AgentdConnection>("agentd_connection");
    } catch (err) {
      const dev = useDev ? devConnection() : null;
      if (dev) return dev;
      throw connectionError(err);
    }
  }

  async function send(path: string, opts: RequestOptions, accept: string): Promise<Response> {
    const conn = await getConnection();
    const headers = new Headers();
    headers.set("Authorization", `Bearer ${conn.token}`);
    headers.set("Accept", accept);
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(opts.body);
    }
    const url = `${conn.base_url}${withQuery(path, opts.query)}`;
    let res: Response;
    try {
      res = await doFetch(url, { method: opts.method ?? "GET", headers, body, signal: opts.signal });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new AgentdError("unreachable", `agentd is not running at ${conn.base_url}`, { path });
    }
    if (res.status === 401) {
      throw new AgentdError(
        "unauthorized",
        `agentd at ${conn.base_url} rejected the token from ${conn.data_dir} ` +
          "(is agentd using a different NEWTON_DATA_DIR?)",
        { status: 401, path },
      );
    }
    if (!res.ok) {
      let parsed: unknown = undefined;
      try {
        parsed = await res.json();
      } catch {
        // not JSON
      }
      const info = parseErrorBody(res.status, parsed, res.statusText || `HTTP ${res.status}`);
      throw new AgentdError("http", info.message, { ...info, path });
    }
    return res;
  }

  async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const res = await send(path, opts, "application/json");
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  async function text(path: string, opts: RequestOptions = {}): Promise<string> {
    return (await send(path, opts, "text/plain, text/markdown, */*")).text();
  }

  /** Files behind the bearer token (report plots): fetched, then shown as blob URLs. */
  async function blob(path: string, opts: RequestOptions = {}): Promise<Blob> {
    return (await send(path, opts, "*/*")).blob();
  }

  return {
    getConnection,
    request,
    text,
    blob,
    health: () => request<Health>("/health"),
    hosts: () => request<Host[]>("/hosts"),
  };
}

export type AgentdClient = ReturnType<typeof createAgentdClient>;

/** The client wired to the real Tauri shell and window.fetch. */
export const agentd = createAgentdClient();
