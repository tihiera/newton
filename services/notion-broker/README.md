# Newton Notion broker

A Cloudflare Worker that holds the Notion integration's **client secret** so the
Newton desktop app never has to. Notion's public integrations need that secret to
turn a sign-in code into a token; a Mac app can't keep a secret, so agentd asks this
broker instead. It has no dependencies, stores nothing and logs nothing.

| Request | Body | Does |
| --- | --- | --- |
| `POST /notion/token` | `{"code", "redirect_uri"}` | Exchanges the code at `https://api.notion.com/v1/oauth/token` (`grant_type: authorization_code`) |
| `POST /notion/refresh` | `{"refresh_token"}` | Same call with `grant_type: refresh_token` |

Both authenticate to Notion with HTTP Basic `base64(NOTION_CLIENT_ID:NOTION_CLIENT_SECRET)`
and return Notion's status and JSON unchanged. The broker answers:

- `400 {"error": "redirect_uri not allowed"}` when `redirect_uri` isn't listed in
  `ALLOWED_REDIRECT_URIS` (exact match), `400` for invalid JSON or a missing field;
- `404` for any other path, `405` for any other method, `413` for bodies over 8 KB;
- `500 {"error": "broker not configured"}` when a setting is missing (it never says
  which, nor echoes any value); `502` when Notion can't be reached.

There are no CORS headers: only agentd (server to server) calls it.

## Configuration

| Name | Kind | Value |
| --- | --- | --- |
| `NOTION_CLIENT_ID` | secret | The integration's OAuth client ID |
| `NOTION_CLIENT_SECRET` | secret | The integration's OAuth client secret |
| `ALLOWED_REDIRECT_URIS` | var | Comma-separated, e.g. `http://127.0.0.1:8765/connectors/notion/callback` |

`ALLOWED_REDIRECT_URIS` must hold exactly the redirect URI(s) registered with the Notion
integration and sent by agentd. Keep the `127.0.0.1` one whenever Notion accepts it.
Only if Notion refuses an IP address, register
`http://localhost:8765/connectors/notion/callback`, list it here, and build agentd with
the same value in `NOTION_REDIRECT_URI`
(`services/agentd/newton_agentd/connectors/oauth_apps.py`; the environment variable
`NEWTON_NOTION_REDIRECT_URI` overrides it). `localhost` can resolve to IPv6 `::1`,
where another local program could listen on the same port and take the sign-in code.

## Deploy

You need a Cloudflare account and Node.js (for `npx wrangler`).

```sh
cd services/notion-broker
npx wrangler login
cp wrangler.toml.example wrangler.toml      # edit name / ALLOWED_REDIRECT_URIS if needed
npx wrangler secret put NOTION_CLIENT_ID     # paste the client ID when asked
npx wrangler secret put NOTION_CLIENT_SECRET # paste the client secret when asked
npx wrangler deploy                          # prints https://newton-notion-broker.<you>.workers.dev
```

`wrangler.toml` is git-ignored. Put the printed URL (no trailing slash) into
`NOTION_BROKER_URL` in `services/agentd/newton_agentd/connectors/oauth_apps.py`, or set
`NEWTON_NOTION_BROKER_URL` for agentd. The full walkthrough, including registering the
Notion integration, is in [docs/connectors-setup.md](../../docs/connectors-setup.md).

Check it is live (expect `405`, then `400 {"error":"redirect_uri not allowed"}`):

```sh
curl -i https://newton-notion-broker.<you>.workers.dev/notion/token
curl -i -X POST -H 'Content-Type: application/json' \
  -d '{"code":"x","redirect_uri":"https://example.com"}' \
  https://newton-notion-broker.<you>.workers.dev/notion/token
```

To rotate the secret: generate a new one in the Notion integration's settings, run
`npx wrangler secret put NOTION_CLIENT_SECRET` again. Nothing else changes.

## Test

```sh
cd services/notion-broker && node --test
```

The tests replace `fetch`, so they never contact Notion. `scripts/test.sh` runs them
when `node` is installed.
