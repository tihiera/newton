// How a host reads on its card. Presentation only: every fact comes from agentd's
// host view (hardware probe + capabilities); nothing here decides placement or support.

import type { Capability, Host } from "../../api";
import { hostStatus } from "../../components/labels";
import type { Tone } from "../../components/labels";
import { bytes } from "../../components/time";

const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);

/** The machine's accelerator (or CPU) name, as probed. */
export function deviceName(host: Host): string | null {
  const hw = host.hardware;
  const caps = host.capabilities;
  const gpu = hw?.gpus?.[0];
  return (
    str(gpu?.name) ??
    str(hw?.apple_gpu?.name) ??
    str(caps?.cuda?.device) ??
    str(caps?.metal?.device) ??
    (typeof hw?.cpu === "string" ? str(hw.cpu) : null)
  );
}

/** "121 GB unified memory" / "64 GB memory", or null before the first probe. */
export function memoryLine(host: Host): string | null {
  const total = host.hardware?.memory?.total;
  if (typeof total !== "number" || !total) return null;
  const unified = Boolean(host.hardware?.apple_gpu) || host.hardware?.gpus?.[0]?.unified_memory === true;
  return `${bytes(total)} ${unified ? "unified memory" : "memory"}`;
}

export function workerVersion(host: Host): string | null {
  return str(host.hardware?.worker?.version);
}

export interface CapabilityChip {
  key: "metal" | "cuda" | "cpu";
  label: string;
  ok: boolean;
  /** agentd's reason, verbatim, when it can't be used. */
  reason?: string;
}

const NAMES = { metal: "Metal", cuda: "CUDA", cpu: "CPU" } as const;

/** Chips for what the host can run. A GPU backend that can't be used is listed only
 *  when the GPU itself was found (its reason then says what is missing); CPU shows
 *  when no GPU backend is usable. */
export function capabilityChips(host: Host): CapabilityChip[] {
  const caps = host.capabilities ?? ({} as Record<string, Capability>);
  const out: CapabilityChip[] = [];
  for (const key of ["cuda", "metal"] as const) {
    const c = caps[key];
    if (!c) continue;
    if (c.ok) {
      const cupy = str(c.cupy);
      out.push({ key, label: key === "cuda" && cupy ? `CUDA · CuPy ${cupy}` : NAMES[key], ok: true });
    } else if (c.device) {
      out.push({ key, label: NAMES[key], ok: false, reason: c.reason });
    }
  }
  if (!out.some((c) => c.ok) && caps.cpu?.ok) out.push({ key: "cpu", label: "CPU", ok: true });
  return out;
}

export function statusChip(host: Host): { label: string; tone: Tone; dot: string } {
  const s = hostStatus(host.status);
  const tone: Tone = s.dot === "ok" ? "green" : s.dot === "bad" ? "red" : s.dot === "warn" ? "yellow" : "gray";
  return { label: s.label, tone, dot: s.dot };
}

/** What the GPU support task is doing, in words; null when there is nothing to say. */
export function gpuTaskLine(host: Host): { text: string; tone: "busy" | "error" } | null {
  const t = host.gpu_task;
  if (!t) return null;
  if (t.state === "running") return { text: "Setting up GPU support…", tone: "busy" };
  if (t.state === "failed") return { text: str(t.error) ?? "GPU support failed.", tone: "error" };
  return null;
}

export function canInstallGpu(host: Host): boolean {
  return (
    host.kind === "ssh" &&
    host.gpu_support !== "off" &&
    !host.capabilities?.cuda?.ok &&
    Boolean(host.capabilities?.cuda?.device) &&
    host.gpu_task?.state !== "running"
  );
}

export function needsConnect(host: Host): boolean {
  return host.kind === "ssh" && host.status !== "online";
}
