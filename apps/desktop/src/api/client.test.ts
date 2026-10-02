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
    expect(err.message).toBe("host not found"); // agentd's sentence, verbatim
    expect(err.path).toBe("/hosts/nope");
  });

  it("sends JSON bodies and query strings", async () => {
    const fetch = vi.fn<FetchFn>(async () => json({ id: "goal-1" }, 201));
    const client = createAgentdClient({ invoke: invokeOk(), fetch });
    await client.request("/goals", { method: "POST", body: { title: "t" }, query: { a: 1, b: undefined } });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8799/goals?a=1");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe('{"title":"t"}');
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
  });

  it("maps FastAPI 422s onto fields", async () => {
    const body = {
      detail: [
        { loc: ["body", "title"], msg: "String should have at least 1 character" },
        { loc: ["body", "keywords", 0], msg: "Value error, bad keyword" },
      ],
    };
    const fetch = vi.fn<FetchFn>(async () => json(body, 422));
    const client = createAgentdClient({ invoke: invokeOk(), fetch });
    const err = await caught(client.request("/goals", { method: "POST", body: {} }));
    expect(err.status).toBe(422);
    expect(err.fields.title).toBe("String should have at least 1 character");
    // A list item is filed under its list (the input) and under its full path.
    expect(err.fields.keywords).toBe("bad keyword");
    expect(err.fields["keywords.0"]).toBe("bad keyword");
    expect(err.fields["0"]).toBeUndefined();
  });

  it("files a nested (non-list) field under its own name only", async () => {
    const body = {
      detail: [{ loc: ["body", "settings", "memory_gb"], msg: "Input should be less than or equal to 1024" }],
    };
    const client = createAgentdClient({ invoke: invokeOk(), fetch: vi.fn<FetchFn>(async () => json(body, 422)) });
    const err = await caught(client.request("/services", { method: "POST", body: {} }));
    expect(err.fields).toEqual({ memory_gb: "Input should be less than or equal to 1024" });
  });

  it("lets a list's own 422 win over one copied from an item", async () => {
    const body = {
      detail: [
        { loc: ["body", "keywords", 2], msg: "bad keyword" },
        { loc: ["body", "keywords"], msg: "List should have at most 10 items" },
      ],
    };
    const client = createAgentdClient({ invoke: invokeOk(), fetch: vi.fn<FetchFn>(async () => json(body, 422)) });
    const err = await caught(client.request("/goals", { method: "POST", body: {} }));
    expect(err.fields.keywords).toBe("List should have at most 10 items");
    expect(err.fields["keywords.2"]).toBe("bad keyword");
  });

  it("keeps the whole body of {error, code, ...} errors", async () => {
    const body = {
      error: "the same scheme was tested in exp-1 (yellow)",
      code: "already_tested",
      experiment_id: "exp-1",
    };
    const client = createAgentdClient({ invoke: invokeOk(), fetch: vi.fn<FetchFn>(async () => json(body, 409)) });
    const err = await caught(client.request("/research/items/p/propose", { method: "POST", body: {} }));
    expect(err.code).toBe("already_tested");
    expect(err.message).toBe("the same scheme was tested in exp-1 (yellow)");
    expect(err.body?.experiment_id).toBe("exp-1");
  });

  it("keeps host-key fingerprints from a 409 and reads router-style errors", async () => {
    const fp = vi.fn<FetchFn>(async () =>
      json({ error: "unknown host key", fingerprints: [{ type: "ssh-ed25519", fingerprint: "SHA256:abc" }] }, 409),
    );
    const err = await caught(createAgentdClient({ invoke: invokeOk(), fetch: fp }).request("/hosts/h/connect"));
    expect(err.fingerprints).toEqual([{ type: "ssh-ed25519", fingerprint: "SHA256:abc" }]);
    const router = vi.fn<FetchFn>(async () =>
      json({ error: { message: "no model service runs 'x'", code: "model_not_found" } }, 404),
    );
    const e2 = await caught(createAgentdClient({ invoke: invokeOk(), fetch: router }).request("/v1/x"));
    expect(e2.message).toBe("no model service runs 'x'");
    expect(e2.code).toBe("model_not_found");
  });
});
