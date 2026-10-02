// Response shapes of the agentd endpoints the shell uses.
//
// Hand-written from services/agentd/newton_agentd/api/routes.py (GET /health) and
// orchestration/hosts.py `host_view` + orchestration/capabilities.py (GET /hosts).
// packages/contracts only exports request models (e.g. HostCreate), not these views.
// Only the fields the UI reads are typed strictly; the rest pass through.

/** Returned by the Tauri command `agentd_connection` (src-tauri/src/agentd.rs). */
export interface AgentdConnection {
  base_url: string;
  token: string;
  data_dir: string;
}

/** Error payload of `agentd_connection`. */
export interface AgentdConnectionError {
  code: "token_missing" | "token_unreadable" | "config";
  message: string;
}

export interface Health {
  status: "ok" | "degraded";
  version: string;
  db: { ok: boolean; schema_version: number; path: string };
  scheduler: { running: boolean; ticks: number };
  uptime_seconds: number;
}

export type BackendName = "cpu" | "cuda" | "metal";

export interface Capability {
  ok: boolean;
  device?: string | null;
  reason?: string;
  [extra: string]: unknown;
}

export interface Host {
  id: string;
  name: string;
  kind: "local" | "ssh";
  status: string;
  ssh_target: string | null;
  last_error: string | null;
  last_checked_at: number | null;
  capabilities: Record<BackendName, Capability>;
  [extra: string]: unknown;
}
