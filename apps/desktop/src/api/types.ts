// Response shapes of the agentd endpoints the app uses.
//
// Hand-written from services/agentd (api/routes.py and the view functions behind it;
// see docs/ui-handoff.md). Only fields the UI reads are typed strictly; anything else
// passes through. packages/contracts holds the request schemas.

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

export type Timestamp = number; // unix seconds

export interface Health {
  status: "ok" | "degraded";
  version: string;
  db: { ok: boolean; schema_version: number; path: string };
  scheduler: { running: boolean; ticks: number };
  uptime_seconds: number;
  /** packaged: running from Newton.app's own runtime; problems: sentences (missing parts). */
  runtime?: { packaged: boolean; resources_ok: boolean; problems: string[] };
}

export type ReadinessKey = "engine" | "cpu" | "metal" | "reader" | "gpu_host" | "keychain";
export type ReadinessState = "ok" | "warn" | "missing";
export type ReadinessAction = "open_models" | "open_compute" | "open_settings" | "new_experiment";

/** GET /readiness: what this Mac can do right now, as agentd sees it. The UI only renders it. */
export interface ReadinessItem {
  key: ReadinessKey;
  state: ReadinessState;
  title: string;
  detail: string;
  action: { kind: ReadinessAction; label: string } | null;
}

export interface Readiness {
  items: ReadinessItem[];
}

export interface AgentdEvent {
  id: number;
  ts: Timestamp;
  entity_type: string;
  entity_id: string;
  kind: string;
  data: Record<string, unknown>;
}

// -- hosts -------------------------------------------------------------------------

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
  last_checked_at: Timestamp | null;
  gpu_support?: "auto" | "off" | "cuda";
  max_parallel_jobs?: number;
  hardware?: HostHardware | null;
  capabilities: Record<BackendName, Capability>;
  gpu_task?: { state: string; [extra: string]: unknown } | null;
  [extra: string]: unknown;
}

export interface HostHardware {
  os?: Record<string, unknown> | string;
  cpu?: Record<string, unknown> | string;
  cpu_count?: number;
  memory?: { total?: number; [extra: string]: unknown };
  gpus?: Array<Record<string, unknown>>;
  apple_gpu?: Record<string, unknown> | null;
  worker?: { version?: string; ok?: boolean; [extra: string]: unknown };
  [extra: string]: unknown;
}

export interface SshConfigHost {
  alias: string;
  source: string;
  hostname: string | null;
  user: string | null;
  port: number | null;
  proxy: string | null;
  config_file: string;
  host_id: string | null;
  addable: boolean;
}

export interface ConnectResult {
  host: Host;
  selftest_job_id: string;
}

// -- goals and papers --------------------------------------------------------------

export type GoalStatus = "active" | "paused" | "archived";

export interface Goal {
  id: string;
  title: string;
  description: string;
  keywords: string[];
  categories: string[];
  poll_hours: number;
  auto_propose: boolean;
  status: GoalStatus;
  last_polled_at: Timestamp | null;
  /** The last poll's failure sentence (e.g. offline), cleared by a successful poll. */
  last_poll_error?: string | null;
  /** When the loop looks again (sooner after a failure). */
  next_poll_at?: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export interface GoalCreate {
  title: string;
  description?: string;
  keywords?: string[];
  categories?: string[];
  poll_hours?: number;
  auto_propose?: boolean;
}

export interface PollSummary {
  goal_id: string;
  found: number;
  new: number;
  relevant: number;
  dismissed: number;
  carded: number;
  proposed: string[];
  skipped: Array<{ item: string; why: string }>;
  error?: string;
}

export type ResearchItemState =
  | "discovered"
  | "triaged"
  | "awaiting_approval"
  | "extracting"
  | "carded"
  | "experiment_planned"
  | "executing"
  | "evaluating"
  | "reported"
  | "dismissed"
  | "failed";

export interface PaperMeta {
  arxiv_id: string;
  title: string;
  abstract: string;
  authors: string[];
  published: string;
  categories: string[];
  url: string;
  /** arXiv's journal reference, when the authors gave one ("J. Comput. Phys. 231 (2012)"). */
  journal_ref?: string | null;
  doi?: string | null;
}

export interface PaperCard {
  relevant: boolean;
  summary: string;
  method: {
    name: string;
    limiter: string;
    second_order_correction: boolean;
    time_integration: string;
    order: number | null;
    max_cfl: number | null;
    tvd: boolean;
  };
  claims: Array<{ kind: string; text: string }>;
  benchmarks: string[];
}

export interface ResearchItem {
  id: string;
  goal_id: string | null;
  kind: "paper";
  title: string;
  source: string;
  external_id: string;
  state: ResearchItemState;
  created_at: Timestamp;
  updated_at: Timestamp;
  data: {
    model?: string;
    found_by?: string;
    paper?: PaperMeta;
    text?: { from: "html" | "pdf"; characters: number; sha256: string; path?: string };
    triage?: { relevant: boolean; why: string; model?: string };
    card?: PaperCard;
    scheme_ir?: SchemeIR | null;
    scheme_ir_digest?: string | null;
    method_digest?: string | null;
    scheme_note?: string;
    extraction?: Record<string, unknown>;
    error?: string;
    proposal_note?: string;
    [extra: string]: unknown;
  };
}

export interface Finding {
  id: string;
  goal_id: string | null;
  research_item_id: string | null;
  experiment_id: string;
  scheme_name: string | null;
  scheme_digest: string | null;
  evidence: Evidence;
  claims: Array<{ claim: string; claimed: unknown; holds: boolean | null }>;
  summary: string;
  created_at: Timestamp;
}

// -- schemes -------------------------------------------------------------------------

export interface SchemeIR {
  name: string;
  description?: string | null;
  source?: string | null;
  flux: { limiter: string; correction: unknown };
  time: { method: "one_step" | "rk"; tableau?: { a: number[][]; b: number[] } | null };
  claims: { order: number; max_cfl: number; tvd: boolean };
}

/** POST /experiments/library: compare built-in schemes against a baseline, no paper needed. */
export interface LibraryExperimentCreate {
  goal_id?: string | null;
  /** Library scheme names (GET /schemes), at least one. */
  candidates: string[];
  baseline?: string;
  initial_condition?: string;
  host_id?: string;
  backend?: string;
}

export interface LibraryScheme {
  name: string;
  document: SchemeIR;
  digest: string;
  hand_written: boolean;
}

// -- experiments, jobs, approvals -------------------------------------------------

export type Evidence = "green" | "yellow" | "red" | "unknown";

export type ExperimentState =
  "awaiting_approval" | "executing" | "evaluating" | "reported" | "failed" | "rejected" | "cancelled";

export interface VariantSpec {
  role: "baseline" | "candidate";
  label: string;
  params: Record<string, unknown> & { scheme: string; scheme_ir?: SchemeIR | null };
}

export interface ExperimentSpec {
  title: string;
  benchmark: string;
  objective: "accuracy" | "performance";
  host_id: string;
  backend: string;
  goal_id?: string | null;
  research_item_id?: string | null;
  hypothesis?: string | null;
  timeout_seconds: number;
  variants: VariantSpec[];
}

export interface Assumption {
  claim: string;
  claimed?: unknown;
  measured?: unknown;
  holds: boolean | null;
  allowance?: number;
}

export interface VariantEvaluation {
  label: string;
  role: "baseline" | "candidate";
  job_id: string;
  job_state: string;
  metrics: Record<string, unknown>;
  backend: string | null;
  device: string | null;
  assumptions: Assumption[];
}

export interface Check {
  name: string;
  passed: boolean | null;
  detail: string;
}

export interface CandidateVerdict {
  label: string;
  evidence: Evidence;
  checks: Check[];
  summary: string;
}

export interface ValidationReport {
  experiment_id: string;
  title: string;
  benchmark: string;
  evidence: Evidence;
  summary: string;
  variants: VariantEvaluation[];
  verdicts: CandidateVerdict[];
  provenance: Record<string, unknown>;
  report_path?: string | null;
}

export type JobState =
  | "pending_approval"
  | "queued"
  | "submitting"
  | "running"
  | "collecting"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "rejected";

export interface BenchmarkRun {
  nx: number;
  dx?: number;
  cells?: number;
  steps?: number;
  l2_error?: number;
  linf_error?: number;
  runtime_s?: number;
  [extra: string]: unknown;
}

export interface Job {
  id: string;
  experiment_id: string | null;
  host_id: string;
  role: string;
  label: string;
  state: JobState;
  attempt: number;
  error: string | null;
  metrics: Record<string, unknown> | null;
  results: {
    scheme?: string;
    backend?: string;
    runs?: BenchmarkRun[];
    environment?: Record<string, unknown>;
    assumptions?: Assumption[];
    plots?: string[];
    [extra: string]: unknown;
  } | null;
  manifest: Record<string, unknown>;
  started_at: Timestamp | null;
  finished_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export interface Experiment {
  id: string;
  goal_id: string | null;
  research_item_id: string | null;
  host_id: string;
  title: string;
  spec: ExperimentSpec;
  state: ExperimentState;
  evaluation: ValidationReport | null;
  evidence: Evidence | null;
  report_path: string | null;
  error: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
  jobs?: Job[];
  approval?: { id: string; status: ApprovalStatus } | null;
}

export interface LogChunk {
  data: string;
  offset: number;
  next_offset: number;
  size?: number;
  eof?: boolean;
}

export type ApprovalStatus = "pending" | "approved" | "rejected";
export type ApprovalKind = "execute_experiment" | "start_service" | "publish_report";

export interface Approval {
  id: string;
  kind: ApprovalKind | string;
  subject_type: string;
  subject_id: string;
  title: string;
  details: Record<string, unknown>;
  status: ApprovalStatus;
  decision_note?: string | null;
  decided_at?: Timestamp | null;
  created_at: Timestamp;
}

// -- models -----------------------------------------------------------------------------

export type ServiceState =
  | "awaiting_approval"
  | "approved"
  | "starting"
  | "ready"
  | "draining"
  | "stopping"
  | "stopped"
  | "failed"
  | "lost"
  | "rejected"
  | "cancelled";

export interface Service {
  id: string;
  host_id: string;
  name: string;
  spec: {
    engine: string;
    model: string;
    revision?: string | null;
    memory_gb: number;
    context_length: number;
    parallel: number;
    [extra: string]: unknown;
  };
  state: ServiceState;
  remote_state: string | null;
  healthy: boolean | null;
  remote_port: number | null;
  local_port: number | null;
  error: string | null;
  ready_at: Timestamp | null;
  endpoint: { base_url: string; auth: "bearer" | "none"; reachable: boolean | null } | null;
  auth_note?: string;
  created_at: Timestamp;
}

/** A model a machine already has (GET /hosts/{id}/models). `where`: Newton's own store
 *  (starts with no download), the machine's own Ollama store (reused when `ready`, else
 *  downloaded again), or the Hugging Face cache (downloaded again). */
export interface MachineModel {
  engine: "ollama" | "mlx" | "vllm";
  model: string;
  revision: string;
  size_bytes: number;
  memory_gb_hint: number;
  where: "newton" | "ollama" | "huggingface";
  ready: boolean;
}

/** A well-known model to start with one click (GET /models/catalog), pinned. */
export interface CatalogModel {
  engine: "ollama";
  model: string;
  revision: string;
  size_bytes: number;
  memory_gb_hint: number;
  note: string;
}

export interface RouterModel {
  id: string;
  object: string;
  newton: {
    revisions: Array<string | null>;
    services: Array<{
      id: string;
      host_id: string;
      state: string;
      engine: string;
      revision: string | null;
      routable: boolean;
      paused: boolean;
      in_flight: number;
      parallel: number;
    }>;
  };
}

export interface RouterLease {
  host_id: string;
  job_id: string;
  granted: boolean;
  in_flight: number;
  waiting_on: string | null;
  caveats: string[];
  [extra: string]: unknown;
}

export interface RouterStatus {
  leases: RouterLease[];
  in_flight: number;
  waiting: number;
  models: RouterModel[];
}

export interface RouterRequest {
  id: string;
  path: string;
  model_requested: string | null;
  service_id: string | null;
  host_id: string | null;
  engine: string | null;
  model: string | null;
  revision: string | null;
  stream: number;
  status: number | null;
  error: string | null;
  attempts: number;
  queued_ms: number | null;
  first_byte_ms: number | null;
  duration_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: Timestamp;
}

// -- profile, publishing --------------------------------------------------------------

export interface Profile {
  display_name: string | null;
  default_model: string | null;
  mac_models: boolean;
  updated_at: Timestamp;
}

export interface RouterCredentials {
  base_url: string;
  api_key: string;
  note?: string;
}

export interface ConnectorAccount {
  /** GitHub login, or the Notion workspace's name. */
  name: string;
  /** Notion workspace icon (emoji or URL), GitHub avatar URL; null when none. */
  icon: string | null;
  /** How it was connected: "oauth" (Connect button), "token" (pasted), "gh" (GitHub CLI). */
  method: "oauth" | "token" | "gh";
  connected_at: number;
  /** Notion only: agentd's renewal of the sign-in was rejected; connect again. Cleared
   *  by a reconnect, a pasted token or Disconnect. */
  needs_reauth?: boolean;
}

export interface Connectors {
  github: boolean;
  notion: boolean;
  /** Who is connected, when agentd knows (null for an older token it never looked up). */
  accounts?: { github: ConnectorAccount | null; notion: ConnectorAccount | null };
  /** Whether the one-click Connect (OAuth) is set up in this build; else paste a token. */
  oauth?: { github: boolean; notion: boolean };
}

/** POST /connectors/github/device: show user_code and open verification_uri. */
export interface GithubDeviceStart {
  user_code: string;
  verification_uri: string;
  expires_at: number;
  interval: number;
}

export type ConnectFlowState = "none" | "pending" | "connected" | "denied" | "expired" | "failed";

/** GET /connectors/github/device and GET /connectors/notion/authorize. */
export interface ConnectFlow {
  state: ConnectFlowState;
  /** agentd's sentence when denied / expired / failed. */
  error?: string | null;
  user_code?: string | null;
  /** While pending: GitHub's device page, or Notion's authorize URL. */
  verification_uri?: string | null;
  expires_at?: number | null;
  account?: ConnectorAccount | null;
}

/** POST /connectors/notion/authorize: open url in the browser. */
export interface NotionAuthorizeStart {
  url: string;
  expires_at: number;
}

/** A Notion page the connected integration can write under (GET /connectors/notion/pages). */
export interface NotionPage {
  id: string;
  title: string;
  url: string | null;
  icon: string | null;
}

export type PublicationState = "awaiting_approval" | "approved" | "publishing" | "published" | "rejected" | "failed";

export interface Publication {
  id: string;
  experiment_id: string;
  target: "github" | "notion";
  destination: Record<string, unknown>;
  state: PublicationState;
  url: string | null;
  error: string | null;
  content_sha256: string;
  created_at: Timestamp;
}
