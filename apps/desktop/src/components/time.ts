// Times from agentd are unix seconds (floats).

export function when(ts: number | null | undefined, now = Date.now()): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const today = new Date(now);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `Today ${time}`;
  const yesterday = new Date(now - 86_400_000);
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function ago(ts: number | null | undefined, now = Date.now()): string {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(now / 1000 - ts));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
  return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
}

/** Numbers as agentd reports them, in a readable form (no rounding of meaning). */
export function num(x: unknown, digits = 3): string {
  if (typeof x !== "number" || !Number.isFinite(x)) return x === null || x === undefined ? "—" : String(x);
  if (x !== 0 && (Math.abs(x) < 1e-3 || Math.abs(x) >= 1e5)) return x.toExponential(2);
  return Number(x.toPrecision(digits)).toString();
}

export function bytes(n: number | null | undefined): string {
  if (!n) return "—";
  return `${Math.round(n / 1024 ** 3)} GB`;
}
