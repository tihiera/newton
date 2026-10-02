import { describe, expect, it } from "vitest";
import type { Host, Profile, RouterStatus } from "../../api";
import { computeStatus, readerStatus } from "./status";

const host = (over: Partial<Host>): Host =>
  ({
    id: "h",
    name: "Spark",
    kind: "ssh",
    status: "online",
    ssh_target: "spark",
    last_error: null,
    last_checked_at: null,
    capabilities: { cpu: { ok: true }, cuda: { ok: true, device: "NVIDIA GB10" }, metal: { ok: false } },
    ...over,
  }) as Host;
const local = host({
  id: "local",
  name: "This Mac",
  kind: "local",
  capabilities: {
    cpu: { ok: true },
    cuda: { ok: false },
    metal: { ok: true, device: "Apple M3 Max" },
  },
});

describe("sidebar status lines", () => {
  it("names the GPU box when it is online with CUDA", () => {
    expect(computeStatus([local, host({})])).toEqual({ label: "Spark · GB10 · Online", dot: "ok" });
  });
  it("says when the GPU box needs attention, and falls back to this Mac", () => {
    expect(computeStatus([local, host({ status: "error:unreachable" })]).dot).toBe("bad");
    expect(computeStatus([local])).toEqual({ label: "This Mac · Metal", dot: "ok" });
    expect(computeStatus(undefined).dot).toBe("hollow");
  });
  it("says this Mac is being checked, not CPU only, before agentd's first check", () => {
    const unchecked = host({
      ...local,
      status: "unknown",
      hardware: null,
      last_checked_at: null,
      capabilities: { cpu: { ok: true }, cuda: { ok: false }, metal: { ok: false } },
    });
    expect(computeStatus([unchecked])).toEqual({ label: "This Mac · checking…", dot: "hollow" });
    const cpuOnly = host({ ...unchecked, status: "online", hardware: { cpu_count: 8 }, last_checked_at: 1 });
    expect(computeStatus([cpuOnly])).toEqual({ label: "This Mac · CPU only", dot: "warn" });
    expect(computeStatus([{ ...unchecked, last_checked_at: 1 }]).label).toBe("This Mac · CPU only");
  });
  const profile = (m: string | null): Profile => ({
    display_name: "x",
    default_model: m,
    mac_models: false,
    updated_at: 0,
  });
  const router = (paused: boolean, routable: boolean): RouterStatus => ({
    leases: [],
    in_flight: 0,
    waiting: 0,
    models: [
      {
        id: "llama3.2:3b",
        object: "model",
        newton: {
          revisions: [],
          services: [
            {
              id: "svc-1",
              host_id: "h",
              state: "ready",
              engine: "ollama",
              revision: null,
              routable,
              paused,
              in_flight: 0,
              parallel: 2,
            },
          ],
        },
      },
    ],
  });
  it("reports the reader model as ready, paused or not served", () => {
    expect(readerStatus(profile("llama3.2:3b"), [], router(false, true))).toEqual({
      label: "Reader · llama3.2:3b",
      dot: "ok",
    });
    expect(readerStatus(profile("llama3.2:3b"), [], router(true, false)).label).toContain("paused");
    expect(readerStatus(profile("qwen:7b"), [], router(false, true)).label).toContain("not served");
    expect(readerStatus(profile(null), [], router(false, true)).label).toBe("Reader · not set");
  });
});
