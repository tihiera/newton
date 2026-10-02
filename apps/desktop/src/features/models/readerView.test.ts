import { describe, expect, it } from "vitest";
import type { Profile, RouterStatus, Service } from "../../api";
import { readerLine } from "./readerView";

const profile = (m: string | null): Profile => ({ display_name: null, default_model: m, mac_models: false, updated_at: 0 });

const route = (over: Partial<RouterStatus["models"][0]["newton"]["services"][0]> = {}) => ({
  id: "svc-1",
  host_id: "host-spark",
  state: "ready",
  engine: "ollama",
  revision: "a80c4f17acd5",
  routable: true,
  paused: false,
  in_flight: 0,
  parallel: 4,
  ...over,
});

const router = (services: ReturnType<typeof route>[]): RouterStatus => ({
  leases: [],
  in_flight: 0,
  waiting: 0,
  models: [{ id: "llama3.2:3b", object: "model", newton: { revisions: [], services } }],
});

describe("readerLine", () => {
  it("not set", () => {
    expect(readerLine(profile(null), undefined, undefined)).toMatchObject({ model: null, label: "Not set" });
  });
  it("ready where the router routes it", () => {
    expect(readerLine(profile("llama3.2:3b"), router([route()]), [])).toMatchObject({ hostId: "host-spark", label: "Ready", tone: "green" });
  });
  it("matches :latest loosely", () => {
    expect(readerLine(profile("llama3.2:3b:latest"), router([route()]), [])).toMatchObject({ label: "Ready" });
  });
  it("paused for a timed run", () => {
    expect(readerLine(profile("llama3.2:3b"), router([route({ routable: false, paused: true })]), [])).toMatchObject({ label: "Paused for a timed run" });
  });
  it("falls back to the service state", () => {
    const svc = { id: "svc-2", host_id: "local", spec: { model: "llama3.2:3b" }, state: "awaiting_approval" } as unknown as Service;
    expect(readerLine(profile("llama3.2:3b"), { leases: [], in_flight: 0, waiting: 0, models: [] }, [svc])).toMatchObject({
      hostId: "local",
      label: "Awaiting approval",
    });
  });
  it("not served", () => {
    expect(readerLine(profile("qwen"), { leases: [], in_flight: 0, waiting: 0, models: [] }, [])).toMatchObject({ label: "Not served" });
  });
});
