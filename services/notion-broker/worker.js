// Newton's Notion OAuth broker: a Cloudflare Worker (ES module, no dependencies).
//
// Notion's public integrations need a client secret to exchange an authorization
// code for a token. A desktop app can't keep a secret, so this tiny worker holds it
// and does only the two token calls on Newton's behalf:
//
//   POST /notion/token   {code, redirect_uri}  -> Notion's /v1/oauth/token response
//   POST /notion/refresh {refresh_token}       -> Notion's /v1/oauth/token response
//
// It stores nothing, logs nothing and sends no CORS headers (agentd calls it
// server to server). Configuration (see wrangler.toml.example):
//   NOTION_CLIENT_ID, NOTION_CLIENT_SECRET  secrets (`wrangler secret put`)
//   ALLOWED_REDIRECT_URIS                   comma-separated exact redirect URIs

export const NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token";
export const MAX_BODY_BYTES = 8 * 1024;

const ROUTES = {
  "/notion/token": tokenRequest,
  "/notion/refresh": refreshRequest,
};

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function json(status, body, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ...extra,
    },
  });
}

function config(env) {
  const clientId = (env && env.NOTION_CLIENT_ID) || "";
  const clientSecret = (env && env.NOTION_CLIENT_SECRET) || "";
  const allowed = String((env && env.ALLOWED_REDIRECT_URIS) || "")
    .split(",")
    .map((uri) => uri.trim())
    .filter(Boolean);
  if (!clientId || !clientSecret || allowed.length === 0) {
    // Never say which value is missing or echo any of them.
    throw new HttpError(500, "broker not configured");
  }
  return { clientId, clientSecret, allowed };
}

// Reads at most MAX_BODY_BYTES; a larger body is refused without buffering it all.
async function readBody(request) {
  const declared = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) {
    throw new HttpError(413, "request body too large");
  }
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel().catch(() => {});
      throw new HttpError(413, "request body too large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

async function readJson(request) {
  const text = await readBody(request);
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw new HttpError(400, "invalid JSON");
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    throw new HttpError(400, "invalid JSON");
  }
  return body;
}

function requireString(body, key) {
  const value = body[key];
  if (typeof value !== "string" || value === "") {
    throw new HttpError(400, `${key} is required`);
  }
  return value;
}

function basicAuth(clientId, clientSecret) {
  const bytes = new TextEncoder().encode(`${clientId}:${clientSecret}`);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `Basic ${btoa(binary)}`;
}

async function tokenRequest(body, cfg) {
  const code = requireString(body, "code");
  const redirectUri = requireString(body, "redirect_uri");
  if (!cfg.allowed.includes(redirectUri)) {
    throw new HttpError(400, "redirect_uri not allowed");
  }
  return { grant_type: "authorization_code", code, redirect_uri: redirectUri };
}

async function refreshRequest(body) {
  const refreshToken = requireString(body, "refresh_token");
  return { grant_type: "refresh_token", refresh_token: refreshToken };
}

// Calls Notion and hands its status and JSON back unchanged.
async function callNotion(payload, cfg) {
  let upstream;
  try {
    upstream = await fetch(NOTION_TOKEN_URL, {
      method: "POST",
      headers: {
        Authorization: basicAuth(cfg.clientId, cfg.clientSecret),
        "Content-Type": "application/json",
        Accept: "application/json",
        "Notion-Version": "2022-06-28",
      },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new HttpError(502, "could not reach Notion");
  }
  const text = await upstream.text();
  const noBody = [101, 204, 205, 304].includes(upstream.status);
  return new Response(noBody ? null : text, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") || "application/json",
      "Cache-Control": "no-store",
    },
  });
}

export default {
  async fetch(request, env) {
    try {
      const handler = ROUTES[new URL(request.url).pathname];
      if (!handler) throw new HttpError(404, "not found");
      if (request.method !== "POST") {
        return json(405, { error: "method not allowed" }, { Allow: "POST" });
      }
      const cfg = config(env);
      const body = await readJson(request);
      const payload = await handler(body, cfg);
      return await callNotion(payload, cfg);
    } catch (err) {
      if (err instanceof HttpError) return json(err.status, { error: err.message });
      // Unexpected: a fixed sentence, never the error (it could carry request data).
      return json(500, { error: "broker error" });
    }
  },
};
