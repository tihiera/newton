// One typed function per agentd endpoint the app uses (docs/ui-handoff.md §5).
// Thin wrappers: no decisions here, only paths, methods and shapes.

import { agentd, type AgentdClient } from "./client";
import type { HostKeyFingerprint } from "./errors";
import type {
  AgentdEvent,
  Approval,
  ConnectResult,
  Connectors,
  Experiment,
  Finding,
  Goal,
  GoalCreate,
  GoalStatus,
  Host,
  Job,
  LibraryScheme,
  LogChunk,
  NotionPage,
  PollSummary,
  Profile,
  Publication,
  ResearchItem,
  RouterCredentials,
  RouterModel,
  RouterRequest,
  RouterStatus,
  Service,
  SshConfigHost,
} from "./types";

export function createApi(c: AgentdClient = agentd) {
  const get = <T>(path: string, query?: Record<string, string | number | undefined | null>, signal?: AbortSignal) =>
    c.request<T>(path, { query, signal });
  const post = <T>(path: string, body?: unknown) => c.request<T>(path, { method: "POST", body: body ?? {} });

  return {
    health: c.health,
    events: (after: number, opts: { limit?: number; entity_type?: string; entity_id?: string } = {}, signal?: AbortSignal) =>
      get<AgentdEvent[]>("/events", { after, ...opts }, signal),

    // -- goals & papers -------------------------------------------------------------
    goals: {
      list: (signal?: AbortSignal) => get<Goal[]>("/goals", undefined, signal),
      get: (id: string) => get<Goal>(`/goals/${id}`),
      create: (body: GoalCreate) => post<Goal>("/goals", body),
      update: (id: string, body: Partial<GoalCreate> & { status?: GoalStatus }) =>
        c.request<Goal>(`/goals/${id}`, { method: "PATCH", body }),
      poll: (id: string) => post<PollSummary>(`/goals/${id}/poll`),
    },
    research: {
      items: (goalId?: string | null, signal?: AbortSignal) =>
        get<ResearchItem[]>("/research/items", { goal_id: goalId ?? undefined }, signal),
      item: (id: string, signal?: AbortSignal) => get<ResearchItem>(`/research/items/${id}`, undefined, signal),
      ingest: (body: { ref: string; goal_id?: string | null; model?: string | null }) =>
        post<ResearchItem>("/research/ingest", body),
      propose: (
        id: string,
        body: {
          host_id?: string;
          backend?: string;
          baseline?: string;
          initial_condition?: string;
          /** Run it although the same scheme was already tested (agentd answers 409
           *  `already_tested` / `already_planned` otherwise). */
          retest?: boolean;
        } = {},
      ) => post<Experiment>(`/research/items/${id}/propose`, body),
      findings: (goalId?: string | null, signal?: AbortSignal) =>
        get<Finding[]>("/findings", { goal_id: goalId ?? undefined }, signal),
      schemes: () => get<LibraryScheme[]>("/schemes"),
    },

    // -- experiments, jobs, approvals ----------------------------------------------
    experiments: {
      list: (state?: string, signal?: AbortSignal) => get<Experiment[]>("/experiments", { state }, signal),
      get: (id: string, signal?: AbortSignal) => get<Experiment>(`/experiments/${id}`, undefined, signal),
      cancel: (id: string) => post<Experiment>(`/experiments/${id}/cancel`),
      report: (id: string) => c.text(`/experiments/${id}/report`),
      reportFile: (id: string, path: string) => c.blob(`/experiments/${id}/report/files/${path}`),
      /** The report as a zip: report.md, report.json (the ValidationReport) and its figures. */
      exportZip: (id: string) => c.blob(`/experiments/${id}/export`),
    },
    jobs: {
      list: (experimentId: string, signal?: AbortSignal) =>
        get<Job[]>("/jobs", { experiment_id: experimentId }, signal),
      get: (id: string) => get<Job>(`/jobs/${id}`),
      logs: (id: string, stream: "stdout" | "stderr", offset: number, signal?: AbortSignal) =>
        get<LogChunk>(`/jobs/${id}/logs`, { stream, offset }, signal),
      artifacts: (id: string) => get<string[]>(`/jobs/${id}/artifacts`),
      artifact: (id: string, path: string) => c.blob(`/jobs/${id}/artifacts/${path}`),
      cancel: (id: string) => post<Job>(`/jobs/${id}/cancel`),
    },
    approvals: {
      pending: (signal?: AbortSignal) => get<Approval[]>("/approvals", { status: "pending" }, signal),
      list: (status?: string) => get<Approval[]>("/approvals", { status }),
      approve: (id: string, note?: string) => post<Approval>(`/approvals/${id}/approve`, note ? { note } : {}),
      reject: (id: string, note?: string) => post<Approval>(`/approvals/${id}/reject`, note ? { note } : {}),
    },

    // -- hosts ----------------------------------------------------------------------
    hosts: {
      list: (signal?: AbortSignal) => get<Host[]>("/hosts", undefined, signal),
      sshConfig: () => get<SshConfigHost[]>("/ssh/hosts"),
      create: (body: { name: string; ssh_target: string; ssh_port?: number | string; gpu_support?: string }) =>
        post<Host>("/hosts", body),
      connect: (id: string) => post<ConnectResult>(`/hosts/${id}/connect`),
      hostkeys: (id: string) => get<{ keys?: unknown[]; [k: string]: unknown }>(`/hosts/${id}/hostkeys`),
      trust: (id: string, fingerprints: string[]) =>
        post<HostKeyFingerprint[]>(`/hosts/${id}/hostkeys/trust`, { fingerprints }),
      check: (id: string) => post<Host>(`/hosts/${id}/check`),
      gpuSupport: (id: string) => post<Record<string, unknown>>(`/hosts/${id}/gpu-support`),
      selftest: (id: string) => post<Job>(`/hosts/${id}/selftest`, {}),
      update: (id: string, body: { gpu_support?: string; max_parallel_jobs?: number }) =>
        c.request<Host>(`/hosts/${id}`, { method: "PATCH", body }),
      remove: (id: string, force = false) =>
        c.request<void>(`/hosts/${id}`, { method: "DELETE", query: { force: force || undefined } }),
    },

    // -- models -----------------------------------------------------------------------
    services: {
      list: (signal?: AbortSignal) => get<Service[]>("/services", undefined, signal),
      create: (body: { host_id: string; name: string; settings: Record<string, unknown> }) =>
        post<Service>("/services", body),
      stop: (id: string) => post<Service>(`/services/${id}/stop`),
      drain: (id: string, seconds: number) =>
        c.request<Service>(`/services/${id}/drain`, { method: "POST", query: { seconds } }),
      logs: (id: string, offset: number) => get<LogChunk>(`/services/${id}/logs`, { offset }),
    },
    router: {
      status: (signal?: AbortSignal) => get<RouterStatus>("/router/status", undefined, signal),
      requests: (limit = 50) => get<RouterRequest[]>("/router/requests", { limit }),
      models: () => get<{ data: RouterModel[] }>("/v1/models"),
      credentials: () => get<RouterCredentials>("/router/credentials"),
      rotate: () => post<RouterCredentials>("/router/credentials/rotate"),
    },

    // -- profile & publishing -----------------------------------------------------------
    profile: {
      get: (signal?: AbortSignal) => get<Profile>("/profile", undefined, signal),
      update: (body: Partial<Pick<Profile, "display_name" | "default_model" | "mac_models">>) =>
        c.request<Profile>("/profile", { method: "PATCH", body }),
    },
    connectors: {
      get: () => get<Connectors>("/connectors"),
      connect: (target: "github" | "notion", token: string) =>
        c.request<Connectors>(`/connectors/${target}`, { method: "PUT", body: { token } }),
      importGh: () => post<Connectors>("/connectors/github/import-gh"),
      notionPages: (query?: string, signal?: AbortSignal) =>
        get<NotionPage[]>("/connectors/notion/pages", { query: query || undefined }, signal),
      disconnect: (target: "github" | "notion") =>
        c.request<Connectors>(`/connectors/${target}`, { method: "DELETE" }),
    },
    publishing: {
      publish: (experimentId: string, target: "github" | "notion", destination: Record<string, unknown>) =>
        post<Publication>(`/experiments/${experimentId}/publish`, { target, destination }),
      list: (experimentId?: string) => get<Publication[]>("/publications", { experiment_id: experimentId }),
    },
  };
}

export type Api = ReturnType<typeof createApi>;

export const api = createApi();
