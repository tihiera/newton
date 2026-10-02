import { describe, expect, it, vi } from "vitest";
import { AgentdError, createAgentdClient, type FetchFn, type InvokeFn } from "./client";

const CONN = { base_url: "http://127.0.0.1:8799", token: "tok-123", data_dir: "/tmp/nd" };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function invokeOk(): InvokeFn {
  return vi.fn(async () => CONN) as unknown as InvokeFn;
}

function invokeFails(payload: unknown): InvokeFn {
  return vi.fn(async () => {
    throw payload;
  }) as unknown as InvokeFn;
}

async function caught(p: Promise<unknown>): Promise<AgentdError> {
  const err = await p.then(
    () => {
      throw new Error("expected a rejection");
    },
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(AgentdError);
  return err as AgentdError;
}

describe("agentd client", () => {
  it("asks the shell for the connection via agentd_connection", async () => {
    const invoke = invokeOk();
    const client = createAgentdClient({ invoke, fetch: vi.fn() });
    await expect(client.getConnection()).resolves.toEqual(CONN);
    expect(invoke).toHaveBeenCalledWith("agentd_connection");
  });

  it("adds the bearer token and calls base_url + path", async () => {
    const fetch = vi.fn<FetchFn>(async () => json([{ id: "local", name: "This Mac" }]));
    const client = createAgentdClient({ invoke: invokeOk(), fetch });

    const hosts = await client.hosts();

    expect(hosts[0].id).toBe("local");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8799/hosts");
    const headers = new Headers(init?.headers);
    expect(headers.get("Authorization")).toBe("Bearer tok-123");
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("GET /health returns the parsed body", async () => {
    const body = { status: "ok", version: "0.1.0" };
    const fetch = vi.fn<FetchFn>(async () => json(body));
    const client = createAgentdClient({ invoke: invokeOk(), fetch });
    await expect(client.health()).resolves.toEqual(body);
    expect(fetch.mock.calls[0][0]).toBe("http://127.0.0.1:8799/health");
  });

  it("maps a missing token file to token_missing without calling fetch", async () => {
    const fetch = vi.fn<FetchFn>();
    const client = createAgentdClient({
      invoke: invokeFails({
        code: "token_missing",
        message: "agentd not started yet: no token file at /tmp/nd/api-token",
      }),
      fetch,
    });

    const err = await caught(client.hosts());

    expect(err.kind).toBe("token_missing");
    expect(err.message).toContain("no token file at /tmp/nd/api-token");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("maps other shell errors and a missing Tauri runtime to config", async () => {
    const bad = createAgentdClient({
      invoke: invokeFails({ code: "config", message: "NEWTON_PORT is not a valid port" }),
      fetch: vi.fn(),
    });
    expect((await caught(bad.health())).kind).toBe("config");

    const noTauri = createAgentdClient({ invoke: invokeFails(new TypeError("x")), fetch: vi.fn() });
    const err = await caught(noTauri.health());
    expect(err.kind).toBe("config");
    expect(err.message).toContain("Tauri");
  });

  it("maps a network failure to unreachable", async () => {
    const fetch = vi.fn<FetchFn>(async () => {
      throw new TypeError("Load failed");
    });
    const client = createAgentdClient({ invoke: invokeOk(), fetch });
    const err = await caught(client.health());
    expect(err.kind).toBe("unreachable");
    expect(err.message).toBe("agentd is not running at http://127.0.0.1:8799");
  });

  it("maps 401 to unauthorized and never leaks the token", async () => {
    const fetch = vi.fn<FetchFn>(async () => json({ error: "unauthorized" }, 401));
    const client = createAgentdClient({ invoke: invokeOk(), fetch });
    const err = await caught(client.hosts());
    expect(err.kind).toBe("unauthorized");
    expect(err.status).toBe(401);
    expect(err.message).not.toContain("tok-123");
  });

  it("surfaces agentd's error text for other HTTP errors", async () => {
    const fetch = vi.fn<FetchFn>(async () => json({ error: "host not found" }, 404));
    const client = createAgentdClient({ invoke: invokeOk(), fetch });
    const err = await caught(client.request("/hosts/nope"));
    expect(err.kind).toBe("http");
    expect(err.status).toBe(404);
    expect(err.message).toBe("/hosts/nope: HTTP 404: host not found");
  });
});
