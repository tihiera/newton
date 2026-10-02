import { describe, expect, it } from "vitest";
import type { Host } from "../../api";
import { canInstallGpu, capabilityChips, deviceName, gpuTaskLine, memoryLine, needsConnect, statusChip } from "./hostView";

const GB = 1024 ** 3;

function host(over: Partial<Host> = {}): Host {
  return {
    id: "host-1",
    name: "Spark",
    kind: "ssh",
    status: "online",
    ssh_target: "spark",
    last_error: null,
    last_checked_at: null,
    gpu_support: "auto",
    hardware: null,
    capabilities: { cpu: { ok: true }, cuda: { ok: false, reason: "host not checked yet" }, metal: { ok: false, reason: "host not checked yet" } },
    gpu_task: null,
    ...over,
  };
}

describe("hostView", () => {
  it("reads the GB10 as the mockup shows it", () => {
    const h = host({
      hardware: { memory: { total: 121 * GB }, gpus: [{ name: "NVIDIA GB10", unified_memory: true }], worker: { version: "0.4.0" } },
      capabilities: {
        cpu: { ok: true, device: "aarch64" },
        cuda: { ok: true, device: "NVIDIA GB10", cupy: "14.2.0" },
        metal: { ok: false, reason: "no Apple GPU on this host" },
      },
    });
    expect(deviceName(h)).toBe("NVIDIA GB10");
    expect(memoryLine(h)).toBe("121 GB unified memory");
    expect(capabilityChips(h)).toEqual([{ key: "cuda", label: "CUDA · CuPy 14.2.0", ok: true }]);
    expect(statusChip(h)).toMatchObject({ label: "Online", tone: "green" });
    expect(canInstallGpu(h)).toBe(false);
  });

  it("shows a found-but-unusable GPU with agentd's reason, and offers GPU support", () => {
    const reason = "NVIDIA GB10 found, but CuPy isn't installed on the host (install GPU support)";
    const h = host({
      hardware: { memory: { total: 64 * GB }, gpus: [{ name: "NVIDIA GB10" }] },
      capabilities: { cpu: { ok: true }, cuda: { ok: false, device: "NVIDIA GB10", reason }, metal: { ok: false, reason: "x" } },
    });
    expect(memoryLine(h)).toBe("64 GB memory");
    expect(capabilityChips(h)).toEqual([
      { key: "cuda", label: "CUDA", ok: false, reason },
      { key: "cpu", label: "CPU", ok: true },
    ]);
    expect(canInstallGpu(h)).toBe(true);
    expect(canInstallGpu({ ...h, gpu_support: "off" })).toBe(false);
    expect(canInstallGpu({ ...h, gpu_task: { state: "running" } })).toBe(false);
  });

  it("an unchecked host lists CPU only and needs connecting", () => {
    const h = host({ status: "error:hostkey_unknown" });
    expect(capabilityChips(h)).toEqual([{ key: "cpu", label: "CPU", ok: true }]);
    expect(needsConnect(h)).toBe(true);
    expect(statusChip(h)).toMatchObject({ label: "Host key not trusted", tone: "red" });
    expect(needsConnect(host({ kind: "local", status: "unknown" }))).toBe(false);
  });

  it("gpu task lines", () => {
    expect(gpuTaskLine(host({ gpu_task: { state: "running" } }))?.text).toBe("Setting up GPU support…");
    expect(gpuTaskLine(host({ gpu_task: { state: "failed", error: "pip failed" } }))).toEqual({ text: "pip failed", tone: "error" });
    expect(gpuTaskLine(host({ gpu_task: { state: "done", ok: true } }))).toBeNull();
  });
});
