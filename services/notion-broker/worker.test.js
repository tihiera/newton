// Tests for the Notion OAuth broker: `node --test services/notion-broker`.
// Notion is never contacted: globalThis.fetch is replaced by a recorder.

import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

import worker, { MAX_BODY_BYTES, NOTION_TOKEN_URL } from "./worker.js";

const CLIENT_ID = "client-id-1234";
const CLIENT_SECRET = "secret_s3cr3t-value";
const REDIRECT = "http://127.0.0.1:8765/connectors/notion/callback";
const ENV = {
  NOTION_CLIENT_ID: CLIENT_ID,
  NOTION_CLIENT_SECRET: CLIENT_SECRET,
  ALLOWED_REDIRECT_URIS: ` ${REDIRECT} , http://localhost:9999/connectors/notion/callback`,
};
const EXPECTED_AUTH = `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString("base64")}`;
const BASE = "https://broker.example.workers.dev";

const realFetch = globalThis.fetch;
let calls;
let reply;

function mockNotion(status, body, headers = { "Content-Type": "application/json" }) {
  reply = () => new Response(JSON.stringify(body), { status, headers });
}

beforeEach(() => {
  calls = [];
  mockNotion(200, {});
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return reply();
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function post(path, body, extra = {}) {
  return new Request(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...extra },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

async function call(request, env = ENV) {
  const response = await worker.fetch(request, env);
  const text = await response.text();
  return { response, text, body: text ? JSON.parse(text) : null };
}

function assertNoSecrets(text) {
  assert.ok(!text.includes(CLIENT_SECRET), "client secret echoed");
  assert.ok(!text.includes(CLIENT_ID), "client id echoed");
  assert.ok(!text.includes(Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString("base64")));
}

describe("POST /notion/token", () => {
  it("exchanges the code with Basic auth and passes Notion's answer through", async () => {
    const notion = {
      access_token: "ntn_access",
      refresh_token: "ntn_refresh",
      expires_in: 3600,
      token_type: "bearer",
      bot_id: "bot-1",
      workspace_name: "Lab",
      workspace_icon: "https://example.com/icon.png",
      workspace_id: "ws-1",
    };
    mockNotion(200, notion);
    const { response, body } = await call(post("/notion/token", { code: "c0de", redirect_uri: REDIRECT }));

    assert.equal(response.status, 200);
    assert.deepEqual(body, notion);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, NOTION_TOKEN_URL);
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers.Authorization, EXPECTED_AUTH);
    assert.equal(calls[0].init.headers["Content-Type"], "application/json");
    assert.equal(
      calls[0].init.body,
      JSON.stringify({ grant_type: "authorization_code", code: "c0de", redirect_uri: REDIRECT }),
    );
  });

  it("passes Notion's error status and JSON through unchanged", async () => {
    const notion = { error: "invalid_grant", error_description: "Invalid code." };
    mockNotion(400, notion);
    const { response, body, text } = await call(post("/notion/token", { code: "used", redirect_uri: REDIRECT }));
    assert.equal(response.status, 400);
    assert.deepEqual(body, notion);
    assertNoSecrets(text);
  });

  it("refuses a redirect_uri that isn't allowed, without calling Notion", async () => {
    const { response, body } = await call(
      post("/notion/token", { code: "c0de", redirect_uri: "https://evil.example/cb" }),
    );
    assert.equal(response.status, 400);
    assert.deepEqual(body, { error: "redirect_uri not allowed" });
    assert.equal(calls.length, 0);
  });

  it("matches redirect URIs exactly", async () => {
    const { response } = await call(post("/notion/token", { code: "c0de", redirect_uri: `${REDIRECT}/` }));
    assert.equal(response.status, 400);
    assert.equal(calls.length, 0);
  });

  it("requires code and redirect_uri", async () => {
    const missing = await call(post("/notion/token", { redirect_uri: REDIRECT }));
    assert.equal(missing.response.status, 400);
    assert.deepEqual(missing.body, { error: "code is required" });
    const noRedirect = await call(post("/notion/token", { code: "c0de" }));
    assert.equal(noRedirect.response.status, 400);
    assert.deepEqual(noRedirect.body, { error: "redirect_uri is required" });
    assert.equal(calls.length, 0);
  });
});

describe("POST /notion/refresh", () => {
  it("refreshes with Basic auth and passes Notion's answer through", async () => {
    const notion = { access_token: "ntn_new", refresh_token: "ntn_refresh2", expires_in: 3600 };
    mockNotion(200, notion);
    const { response, body } = await call(post("/notion/refresh", { refresh_token: "ntn_refresh" }));

    assert.equal(response.status, 200);
    assert.deepEqual(body, notion);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, NOTION_TOKEN_URL);
    assert.equal(calls[0].init.headers.Authorization, EXPECTED_AUTH);
    assert.equal(calls[0].init.body, JSON.stringify({ grant_type: "refresh_token", refresh_token: "ntn_refresh" }));
  });

  it("passes a 401 from Notion through", async () => {
    mockNotion(401, { error: "invalid_grant" });
    const { response, body } = await call(post("/notion/refresh", { refresh_token: "old" }));
    assert.equal(response.status, 401);
    assert.deepEqual(body, { error: "invalid_grant" });
  });

  it("requires refresh_token", async () => {
    const { response, body } = await call(post("/notion/refresh", {}));
    assert.equal(response.status, 400);
    assert.deepEqual(body, { error: "refresh_token is required" });
    assert.equal(calls.length, 0);
  });
});

describe("request validation", () => {
  it("answers 405 to other methods on a known path", async () => {
    for (const method of ["GET", "PUT", "DELETE", "OPTIONS"]) {
      const { response, body } = await call(new Request(`${BASE}/notion/token`, { method }));
      assert.equal(response.status, 405, method);
      assert.equal(response.headers.get("Allow"), "POST");
      assert.deepEqual(body, { error: "method not allowed" });
    }
    assert.equal(calls.length, 0);
  });

  it("answers 404 to unknown paths", async () => {
    for (const path of ["/", "/notion", "/notion/token/", "/oauth/token", "/notion/revoke"]) {
      const { response, body } = await call(post(path, { code: "c0de", redirect_uri: REDIRECT }));
      assert.equal(response.status, 404, path);
      assert.deepEqual(body, { error: "not found" });
    }
    assert.equal(calls.length, 0);
  });

  it("answers 413 to bodies over 8 KB", async () => {
    const big = JSON.stringify({ code: "x".repeat(MAX_BODY_BYTES), redirect_uri: REDIRECT });
    const { response, body } = await call(post("/notion/token", big));
    assert.equal(response.status, 413);
    assert.deepEqual(body, { error: "request body too large" });
    assert.equal(calls.length, 0);
  });

  it("answers 413 when the body is over 8 KB without a Content-Length", async () => {
    const chunk = new TextEncoder().encode("x".repeat(4096));
    const stream = new ReadableStream({
      start(controller) {
        for (let i = 0; i < 3; i += 1) controller.enqueue(chunk);
        controller.close();
      },
    });
    const request = new Request(`${BASE}/notion/refresh`, {
      method: "POST",
      body: stream,
      duplex: "half",
    });
    assert.equal(request.headers.get("Content-Length"), null);
    const { response } = await call(request);
    assert.equal(response.status, 413);
    assert.equal(calls.length, 0);
  });

  it("answers 400 to invalid JSON", async () => {
    for (const raw of ["{not json", "", "[]", "null", '"code"']) {
      const { response, body } = await call(post("/notion/token", raw));
      assert.equal(response.status, 400, raw);
      assert.deepEqual(body, { error: "invalid JSON" });
    }
    assert.equal(calls.length, 0);
  });

  it("answers 500 without naming or leaking values when configuration is missing", async () => {
    const partials = [
      {},
      { NOTION_CLIENT_ID: CLIENT_ID, NOTION_CLIENT_SECRET: CLIENT_SECRET },
      { NOTION_CLIENT_ID: CLIENT_ID, ALLOWED_REDIRECT_URIS: REDIRECT },
      { NOTION_CLIENT_SECRET: CLIENT_SECRET, ALLOWED_REDIRECT_URIS: REDIRECT },
      { ...ENV, ALLOWED_REDIRECT_URIS: " , " },
    ];
    for (const env of partials) {
      const { response, body, text } = await call(post("/notion/token", { code: "c0de", redirect_uri: REDIRECT }), env);
      assert.equal(response.status, 500);
      assert.deepEqual(body, { error: "broker not configured" });
      assertNoSecrets(text);
      assert.ok(!text.includes("NOTION_"));
    }
    assert.equal(calls.length, 0);
  });

  it("answers 502 with a fixed sentence when Notion can't be reached", async () => {
    reply = () => {
      throw new TypeError(`fetch failed: ${CLIENT_SECRET}`);
    };
    const { response, body, text } = await call(post("/notion/refresh", { refresh_token: "r" }));
    assert.equal(response.status, 502);
    assert.deepEqual(body, { error: "could not reach Notion" });
    assertNoSecrets(text);
  });
});

describe("secrets", () => {
  it("never echoes the client id or secret in any response", async () => {
    const requests = [
      post("/notion/token", { code: "c0de", redirect_uri: REDIRECT }),
      post("/notion/token", { code: "c0de", redirect_uri: "https://evil.example" }),
      post("/notion/refresh", { refresh_token: "r" }),
      post("/notion/refresh", "{bad"),
      post("/nope", {}),
      new Request(`${BASE}/notion/token`, { method: "GET" }),
    ];
    mockNotion(200, { access_token: "ntn_access" });
    for (const request of requests) {
      const { response, text } = await call(request);
      assertNoSecrets(text);
      for (const [, value] of response.headers) assertNoSecrets(value);
    }
  });

  it("writes nothing to the console", async () => {
    const original = { ...console };
    const lines = [];
    for (const level of ["log", "info", "warn", "error", "debug"]) {
      console[level] = (...args) => lines.push(args);
    }
    try {
      await call(post("/notion/token", { code: "c0de", redirect_uri: REDIRECT }));
      await call(post("/notion/token", "{bad"));
      reply = () => {
        throw new Error("down");
      };
      await call(post("/notion/refresh", { refresh_token: "r" }));
    } finally {
      Object.assign(console, original);
    }
    assert.deepEqual(lines, []);
  });
});
