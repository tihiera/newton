# Setting up one-click Connect for GitHub and Notion

For the project owner. Newton's Settings > Connections can sign users in to GitHub and
Notion with a **Connect** button instead of a pasted token. That needs three things
registered once: a GitHub OAuth App, a Notion public integration, and a tiny broker
that holds Notion's client secret. Until they are set, Connect says it isn't set up in
this build and pasting a token keeps working.

All the values Newton needs are public and live in
`services/agentd/newton_agentd/connectors/oauth_apps.py`:

| Constant | Environment override | Where it comes from |
| --- | --- | --- |
| `GITHUB_CLIENT_ID` | `NEWTON_GITHUB_CLIENT_ID` | Step 1 |
| `NOTION_CLIENT_ID` | `NEWTON_NOTION_CLIENT_ID` | Step 2 |
| `NOTION_BROKER_URL` | `NEWTON_NOTION_BROKER_URL` | Step 3 |
| `NOTION_REDIRECT_URI` | `NEWTON_NOTION_REDIRECT_URI` | Step 2, only for the `localhost` fallback; empty means `http://127.0.0.1:{port}/connectors/notion/callback` |

No secret ever goes in this file, in the repo or in the app.

## 1. GitHub: an OAuth App with device flow

1. On github.com, open **Settings > Developer settings > OAuth Apps > New OAuth App**
   (use an organization's settings instead if the app should belong to one).
2. Fill in:
   - **Application name:** Newton.
   - **Homepage URL:** any page, e.g. the repository URL.
   - **Authorization callback URL:** the repository URL. The device flow never uses
     it, but GitHub requires a value.
3. Register the app, then tick **Enable Device Flow** and save.
4. Copy the **Client ID** into `GITHUB_CLIENT_ID` (or set `NEWTON_GITHUB_CLIENT_ID`).
   Do not generate a client secret: the device flow doesn't need one and Newton has
   nowhere safe to keep it.

Newton asks for the scopes `gist` and `public_repo`: reports as gists, and issues in
public repositories only. Users can see and revoke the app at
<https://github.com/settings/applications>.

## 2. Notion: a public integration

1. Go to <https://www.notion.so/profile/integrations> and create a new integration of
   type **Public** (an internal integration can't do OAuth).
2. Capabilities: read content, update content and insert content. Newton doesn't need
   user information.
3. Redirect URI: exactly

   ```
   http://127.0.0.1:8765/connectors/notion/callback
   ```

   Notion matches it character for character. 8765 is agentd's default port; if a user
   runs agentd on another port (`NEWTON_PORT`), that port's URI must be registered here
   and allowed in the broker too, and that user sets `NEWTON_NOTION_REDIRECT_URI` to it
   (the built-in URI names port 8765).

   **Only if Notion refuses an IP address:** register
   `http://localhost:8765/connectors/notion/callback` instead, put the same value into
   `NOTION_REDIRECT_URI` in `oauth_apps.py` (built into the app; the environment
   variable `NEWTON_NOTION_REDIRECT_URI` overrides it) and add it to the broker's
   `ALLOWED_REDIRECT_URIS` (step 3). Prefer `127.0.0.1` whenever Notion accepts it:
   agentd listens on IPv4 only, and `localhost` can resolve to IPv6 `::1`, where
   another local program could listen on the same port and take the sign-in code.
4. Notion asks public integrations for some details about the publisher (such as a
   website, privacy policy and support email); fill them in.
5. Save, then copy the **OAuth client ID** and **OAuth client secret** from the
   integration's settings. The client ID goes into `NOTION_CLIENT_ID` (or
   `NEWTON_NOTION_CLIENT_ID`). The secret goes **only** into the broker (step 3).

## 3. Deploy the Notion broker

The broker (`services/notion-broker`) is a dependency-free Cloudflare Worker that turns
Notion's sign-in code into a token using the client secret. You need a Cloudflare
account and Node.js.

```sh
cd services/notion-broker
npx wrangler login
cp wrangler.toml.example wrangler.toml       # git-ignored
npx wrangler secret put NOTION_CLIENT_ID      # paste the client ID
npx wrangler secret put NOTION_CLIENT_SECRET  # paste the client secret
npx wrangler deploy
```

`ALLOWED_REDIRECT_URIS` in `wrangler.toml` already holds the default redirect URI; it
is a comma-separated list and must contain exactly the URI(s) registered in step 2
(the `localhost` one too, if you used the fallback).
`wrangler deploy` prints the worker URL, e.g.
`https://newton-notion-broker.<account>.workers.dev`. Put it (no trailing slash) into
`NOTION_BROKER_URL` (or `NEWTON_NOTION_BROKER_URL`).

Notion's Connect stays unavailable until both `NOTION_CLIENT_ID` and
`NOTION_BROKER_URL` are set. Restart agentd after changing the constants or the
environment. `services/notion-broker/README.md` has the broker's API and a quick
`curl` check.

## 4. What users see

- **GitHub.** Connect shows a short code and opens GitHub's device page in the browser.
  The user enters the code and approves; Settings then shows their GitHub login (no
  avatar). If they decline, or the code expires (about 15 minutes), Settings says so and
  they can start again.
- **GitHub repositories.** Connect files issues in public repositories only (scope
  `public_repo`). For a private repository, GitHub answers "not found" and the
  publication fails with "GitHub couldn't find owner/name: Connect reaches public
  repositories only; paste a token with repo access for private ones". Users who
  publish to private repositories paste a token with repo access, or import the GitHub
  CLI's login.
- **Notion.** Connect opens Notion's consent page in the browser. The user picks the
  workspace and the pages Newton may use; Newton only ever sees the pages shared there.
  The browser lands on a local page saying "Notion is connected. You can close this
  tab and go back to Newton." and Settings shows the workspace name, with its icon when
  that is an emoji (no images).
- **Notion's sign-in expiring.** Notion's access token expires; agentd renews it through
  the broker. If Notion refuses to renew it (the grant was revoked, say), Settings says
  Notion's sign-in expired and asks to connect Notion again. If Notion or the broker
  just can't be reached, nothing changes and the user can try again in a moment.
- **Paste a token** remains available for both, e.g. for a fine-grained GitHub token
  or a Notion internal integration.

## 5. Security notes

- The Notion client secret exists only in the broker's Cloudflare secrets. The GitHub
  app has no secret at all. The repo and the app hold only public client IDs and the
  broker URL.
- The broker stores nothing and logs nothing, sends no CORS headers, accepts only the
  two token calls, and refuses redirect URIs that aren't on its allow list. Keep the
  Worker's logs off (`[observability] enabled = false` in `wrangler.toml`).
- Tokens go straight into the Mac's Keychain (agentd's secret store). They are never
  written to SQLite, never logged and never returned by the API. The Notion sign-in
  state is single-use, expires after 10 minutes and is kept in memory only.
- Disconnecting in Settings makes Newton forget the token (and Notion's refresh token).
  It doesn't revoke the grant: users revoke Newton at
  <https://github.com/settings/applications> for GitHub, and in Notion's settings
  (connections) for Notion.
- If the Notion client secret leaks, generate a new one in the integration's settings
  and run `npx wrangler secret put NOTION_CLIENT_SECRET` again.

## Testing locally

`node --test` in `services/notion-broker` tests the broker with a mocked `fetch`
(`scripts/test.sh` runs it when `node` is installed). For an end-to-end run against
fakes, agentd's base URLs can be pointed elsewhere: `NEWTON_GITHUB_WEB` (default
`https://github.com`), `NEWTON_GITHUB_API` (`https://api.github.com`) and
`NEWTON_NOTION_API` (`https://api.notion.com/v1`). These are for tests only.
