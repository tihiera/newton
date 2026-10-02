// What can go wrong talking to agentd, as one error type the UI can show.
//
// agentd's own messages are written for people: `message` is that sentence, verbatim.
// 422 validation errors also carry `fields` (input name -> message) so forms can put
// each message next to its input. Never put the token into a message.

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

export interface AgentdErrorInfo {
  status?: number;
  code?: string;
  path?: string;
  /** 422: input name (last element of `loc`) -> message. */
  fields?: Record<string, string>;
  /** 409 when a host key isn't trusted yet: the keys to show the user. */
  fingerprints?: HostKeyFingerprint[];
  /** The whole `{error, code, ...}` body, for the extra fields some errors carry
   *  (e.g. `experiment_id` on a 409 `already_tested`). */
  body?: Record<string, unknown>;
}

/** One host key as agentd reports it: `{type: "ssh-ed25519", fingerprint: "SHA256:…"}`. */
export interface HostKeyFingerprint {
  type: string;
  fingerprint: string;
}

function hostKeys(v: unknown): HostKeyFingerprint[] | undefined {
  if (!Array.isArray(v)) return undefined;
  return v.map((k) => {
    const o = k && typeof k === "object" ? (k as Record<string, unknown>) : null;
    return o ? { type: String(o.type ?? ""), fingerprint: String(o.fingerprint ?? "") } : { type: "", fingerprint: String(k) };
  });
}

export class AgentdError extends Error {
  readonly kind: AgentdErrorKind;
  readonly status?: number;
  readonly code?: string;
  readonly path?: string;
  readonly fields: Record<string, string>;
  readonly fingerprints?: HostKeyFingerprint[];
  readonly body?: Record<string, unknown>;

  constructor(kind: AgentdErrorKind, message: string, info: AgentdErrorInfo | number = {}) {
    super(message);
    const i: AgentdErrorInfo = typeof info === "number" ? { status: info } : info;
    this.name = "AgentdError";
    this.kind = kind;
    this.status = i.status;
    this.code = i.code;
    this.path = i.path;
    this.fields = i.fields ?? {};
    this.fingerprints = i.fingerprints;
    this.body = i.body;
  }

  /** The connection itself is down (vs one request failing). */
  get isConnection(): boolean {
    return this.kind !== "http";
  }
}

interface ValidationItem {
  loc?: Array<string | number>;
  msg?: string;
}

/** The human sentence and structured parts of an agentd error body. Handles
 *  `{error, code?}`, the router's `{error: {message, code}}` and FastAPI 422s. */
export function parseErrorBody(status: number, body: unknown, fallback: string): AgentdErrorInfo & {
  message: string;
} {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    if (Array.isArray(b.detail)) {
      const fields: Record<string, string> = {};
      const fromItem = new Set<string>(); // list names whose message came from an item
      const lines: string[] = [];
      for (const raw of b.detail as ValidationItem[]) {
        const loc = (raw.loc ?? []).filter((p) => p !== "body");
        // A list item (`keywords.1`) is filed under its list's name, so the input that
        // holds the list shows it, and under its full path, so the item can be named.
        const named = loc.filter((p) => typeof p !== "number" && !/^\d+$/.test(String(p)));
        const field = named.length ? String(named[named.length - 1]) : "";
        const path = loc.join(".");
        const msg = String(raw.msg ?? "invalid value").replace(/^Value error, /, "");
        // A list item has an index in its path; a nested field (settings.memory_gb) doesn't.
        const isItem = loc.some((p) => typeof p === "number" || /^\d+$/.test(String(p)));
        // The list's own error wins over one copied from an item.
        if (field && (!(field in fields) || (!isItem && fromItem.has(field)))) {
          fields[field] = msg;
          if (isItem) fromItem.add(field);
          else fromItem.delete(field);
        }
        if (path && isItem && !(path in fields)) fields[path] = msg;
        lines.push(field ? `${loc.join(".")}: ${msg}` : msg);
      }
      return { status, fields, message: lines.join("; ") || fallback };
    }
    if (typeof b.detail === "string") return { status, message: b.detail };
    const err = b.error;
    if (err && typeof err === "object") {
      const e = err as Record<string, unknown>;
      return { status, code: e.code ? String(e.code) : undefined, message: String(e.message ?? fallback) };
    }
    if (typeof err === "string") {
      return {
        status,
        message: err,
        body: b,
        code: typeof b.code === "string" ? b.code : undefined,
        fingerprints: hostKeys(b.fingerprints),
      };
    }
  }
  return { status, message: fallback };
}

/** A sentence for any thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof AgentdError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
