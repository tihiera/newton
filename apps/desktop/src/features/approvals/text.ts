// Wording and small readers for approval `details`. Presentation only: every value
// comes from agentd; nothing here decides whether something should run.

export interface KindText {
  title: string;
  subtitle: string;
  icon: string;
  tile: "" | "blush" | "lavender" | "powder" | "mint";
  approve: string;
  approveIcon?: string;
  chip: string;
}

const KINDS: Record<string, KindText> = {
  execute_experiment: {
    title: "Review experiment",
    subtitle: "Nothing runs until you approve.",
    icon: "flask",
    tile: "",
    approve: "Approve & run",
    approveIcon: "play",
    chip: "Experiment",
  },
  start_service: {
    title: "Start model service",
    subtitle: "Nothing downloads or starts until you approve.",
    icon: "model",
    tile: "lavender",
    approve: "Approve & start",
    approveIcon: "play",
    chip: "Model service",
  },
  publish_report: {
    title: "Approve publication",
    subtitle: "Review the exact content before it leaves this Mac.",
    icon: "share",
    tile: "blush",
    approve: "Approve & publish",
    chip: "Publication",
  },
};

export function kindText(kind: string): KindText {
  return (
    KINDS[kind] ?? {
      title: "Review request",
      subtitle: "Nothing happens until you approve.",
      icon: "shield",
      tile: "powder",
      approve: "Approve",
      chip: kind.replace(/_/g, " "),
    }
  );
}

/** "81a9…e4c2" for a long hex digest; short strings unchanged. */
export function shortHash(s: unknown, head = 4, tail = 4): string {
  if (typeof s !== "string" || !s) return "—";
  const hex = s.includes(":") ? s.slice(s.indexOf(":") + 1) : s;
  if (hex.length <= head + tail + 1) return hex;
  return `${hex.slice(0, head)}…${hex.slice(-tail)}`;
}

/** A git commit as `git log --oneline` shows it. */
export function shortCommit(s: unknown): string {
  return typeof s === "string" && s ? s.slice(0, 7) : "—";
}

export function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

export function numOrUndef(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

export function obj(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

/** Any detail value as text, for the generic list (nothing is hidden). */
export function plain(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "string" || typeof v === "number") return String(v);
  return JSON.stringify(v, null, 2);
}

/** "estimated_peak_gb" -> "Estimated peak gb" (keys of unknown detail kinds). */
export function humanKey(k: string): string {
  const s = k.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** The scheme a variant runs, as its params name it: a library name, or a SchemeIR
 *  document (its own name plus the digest agentd computed). */
export function variantScheme(v: Record<string, unknown>): { scheme: string; digest?: string } {
  const params = obj(v.params) ?? {};
  const scheme = str(params.scheme) ?? "—";
  const ir = obj(params.scheme_ir);
  const digest = str(v.scheme_ir_digest);
  if (scheme === "ir") return { scheme: str(ir?.name) ?? "SchemeIR document", digest };
  return { scheme, digest };
}

export interface DestinationFacts {
  destination: string;
  icon: string;
  whereLabel: string | null;
  where: string | null;
}

/** How a publish_report destination reads ({target, destination} from details). */
export function destinationFacts(target: unknown, destination: unknown): DestinationFacts {
  const d = obj(destination) ?? {};
  if (target === "notion") {
    return { destination: "Notion", icon: "notion", whereLabel: "Parent page", where: str(d.parent_page_id) ?? null };
  }
  if (target === "github" && d.kind === "issue") {
    return { destination: "GitHub Issue", icon: "github", whereLabel: "Repository", where: str(d.repo) ?? null };
  }
  if (target === "github") {
    return { destination: "GitHub Gist", icon: "github", whereLabel: null, where: null };
  }
  return { destination: plain(target), icon: "share", whereLabel: null, where: null };
}

/** The experiment an approval is about (to open it), or null for anything else. */
export function approvalExperimentId(a: { subject_type: string; subject_id: string }): string | null {
  return a.subject_type === "experiment" && a.subject_id ? a.subject_id : null;
}
