// Opt-in check against a running agentd (Node's fetch, so no CORS involved):
//   NEWTON_LIVE_AGENTD_DATA_DIR=<data dir> NEWTON_LIVE_AGENTD_PORT=8799 pnpm test
// The connection is built the way the Rust shell builds it (token from
// <data_dir>/api-token); the Rust side is covered by `cargo test -- --ignored live_agentd`.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { AgentdError, createAgentdClient, type InvokeFn } from "./client";

const dataDir = process.env.NEWTON_LIVE_AGENTD_DATA_DIR;
const port = process.env.NEWTON_LIVE_AGENTD_PORT ?? "8765";

describe.skipIf(!dataDir)("agentd client against a live agentd", () => {
  const conn = () => ({
    base_url: `http://127.0.0.1:${port}`,
    token: readFileSync(join(dataDir!, "api-token"), "utf8").trim(),
    data_dir: dataDir!,
  });
  const invoke = (async () => conn()) as unknown as InvokeFn;

  it("reads /health and /hosts", async () => {
    const client = createAgentdClient({ invoke });
    const health = await client.health();
    expect(health.status).toBe("ok");
    const hosts = await client.hosts();
    const local = hosts.find((h) => h.kind === "local");
    expect(local?.capabilities.cpu.ok).toBe(true);
  });

  it("is rejected with a wrong token", async () => {
    const wrong = (async () => ({ ...conn(), token: "wrong" })) as unknown as InvokeFn;
    const err = await createAgentdClient({ invoke: wrong })
      .hosts()
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AgentdError);
    expect((err as AgentdError).kind).toBe("unauthorized");
  });
});
