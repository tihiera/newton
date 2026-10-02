// One-line summaries for the sidebar's status rows, from what agentd reports.
// Presentation only: which host to name, which words and dot to show.

import type { Host, Profile, RouterStatus, Service } from "../../api";

export interface StatusLine {
  label: string;
  dot: "ok" | "warn" | "bad" | "hollow";
}

function shortDevice(device: string | null | undefined): string | null {
  if (!device) return null;
  return device.replace(/^NVIDIA\s+/i, "").replace(/^Apple\s+/i, "");
}

export function computeStatus(hosts: Host[] | undefined): StatusLine {
  if (!hosts) return { label: "Compute…", dot: "hollow" };
  const remote = hosts.filter((h) => h.kind === "ssh");
  const gpu = remote.find((h) => h.status === "online" && h.capabilities?.cuda?.ok);
  if (gpu) {
    const dev = shortDevice(gpu.capabilities.cuda.device);
    return { label: `${gpu.name}${dev ? ` · ${dev}` : ""} · Online`, dot: "ok" };
  }
  const online = remote.find((h) => h.status === "online");
  if (online) return { label: `${online.name} · Online`, dot: "ok" };
  if (remote.length) {
    const h = remote[0];
    return {
      label: `${h.name} · ${h.status.startsWith("error") ? "Needs attention" : "Not connected"}`,
      dot: h.status.startsWith("error") ? "bad" : "warn",
    };
  }
  const local = hosts.find((h) => h.kind === "local");
  // Before agentd's first check of this Mac its capabilities say nothing yet: "CPU only"
  // would contradict /readiness, which probes Metal itself.
  if (local && local.status === "unknown" && !local.last_checked_at) {
    return { label: "This Mac · checking…", dot: "hollow" };
  }
  if (local?.capabilities?.metal?.ok) return { label: "This Mac · Metal", dot: "ok" };
  return { label: "This Mac · CPU only", dot: "warn" };
}

const plain = (m: string) => m.replace(/:latest$/, "");

export function readerStatus(
  profile: Profile | undefined,
  services: Service[] | undefined,
  router: RouterStatus | undefined,
): StatusLine {
  const model = profile?.default_model;
  if (!profile) return { label: "Reader…", dot: "hollow" };
  if (!model) return { label: "Reader · not set", dot: "hollow" };
  const entry = router?.models.find(
    (m) => m.id === model || plain(m.id) === plain(model) || m.newton.services.some((s) => s.id === model),
  );
  const routes = entry?.newton.services ?? [];
  if (routes.some((s) => s.routable)) return { label: `Reader · ${plain(model)}`, dot: "ok" };
  if (routes.some((s) => s.paused)) return { label: `Reader · ${plain(model)} · paused`, dot: "warn" };
  const starting = (services ?? []).some(
    (s) =>
      (s.spec.model === model || plain(s.spec.model) === plain(model)) &&
      ["approved", "starting", "awaiting_approval"].includes(s.state),
  );
  if (starting || routes.length) return { label: `Reader · ${plain(model)} · starting`, dot: "warn" };
  return { label: `Reader · ${plain(model)} · not served`, dot: "hollow" };
}
