# Newton desktop (Tauri v2)

macOS shell over the agentd HTTP API. The UI only shows what agentd returns. All
product logic stays in `services/agentd`.

**Status: U1 shell only.** `src/App.tsx` and `src/placeholder.css` are
**placeholders** until the real designs land: a "Newton" header, an agentd connection
indicator, and a plain host list (name, kind, status, CUDA/Metal ok or the reason
why not), refreshed every 3 s.

## Run

```sh
# 1. Start agentd (repo root). It uses the repo-local data dir .data/
scripts/dev.sh

# 2. Start the app (apps/desktop)
pnpm install
pnpm dev:repo      # tauri dev, reading the token from <repo>/.data (matches scripts/dev.sh)
pnpm dev           # tauri dev, using the default data dir (~/Library/Application Support/Newton)
```

| Script                       | What it does                                                |
| ---------------------------- | ----------------------------------------------------------- |
| `pnpm dev` / `pnpm dev:repo` | `tauri dev` (Vite on :1420 plus the native window)          |
| `pnpm build`                 | Type-check and build the frontend into `dist/`              |
| `pnpm bundle`                | `tauri build` (Newton.app + dmg)                            |
| `pnpm typecheck`             | `tsc` for `src/` and `vite.config.ts`                       |
| `pnpm test`                  | vitest (API client, with fetch and the Tauri invoke mocked) |

Rust tests: `cd src-tauri && cargo test`.

**Notarization.** If `APPLE_ID`, `APPLE_PASSWORD` and `APPLE_TEAM_ID` are set in your
shell, `tauri build` signs **and notarizes** the bundle. For local bundles, run
`env -u APPLE_ID -u APPLE_PASSWORD -u APPLE_TEAM_ID pnpm tauri build --debug --bundles app`.

## How the UI reaches agentd

- agentd listens on `http://127.0.0.1:$NEWTON_PORT` (default 8765) and requires
  `Authorization: Bearer <token>` on everything except `/health`.
- On its first start, agentd writes the token to `<data_dir>/api-token` with mode 0600.
  `data_dir` is `$NEWTON_DATA_DIR`, or by default `~/Library/Application Support/Newton`.
- The Rust command `agentd_connection` (`src-tauri/src/agentd.rs`) resolves the data
  dir and port with the same rules as `services/agentd/newton_agentd/config.py`. It
  honours `NEWTON_API_TOKEN` too. It reads the token and returns
  `{ base_url, token, data_dir }`. On failure it returns `{ code, message }`, for example
  `token_missing`: "agentd not started yet: no token file at …". The command re-reads
  the token on every call, so agentd can be started after the app. The token is
  never logged: `Connection`'s Debug output redacts it.
- `src/api/client.ts` calls that command, adds the bearer header and maps failures to
  `token_missing | config | unreachable | unauthorized | http`. `src/api/types.ts`
  holds hand-written types for `/health` and `/hosts`. `packages/contracts` has only
  the request schemas.
- Security:
  - The CSP (`src-tauri/tauri.conf.json`) allows `connect-src` only to Tauri IPC and
    `http://127.0.0.1:*`.
  - The command is gated by the capability ACL (`src-tauri/capabilities/default.json`
    allows `allow-agentd-connection`).
  - agentd's CORS list already includes `tauri://localhost` (the bundled app) and
    `http://localhost:1420` (`tauri dev`).
- Apps launched from Finder don't inherit shell env vars, so they use the default
  data dir and port.

Live check against a running agentd (e.g.
`NEWTON_DATA_DIR=$(mktemp -d) uv run newton-agentd serve --port 8799`):

```sh
NEWTON_LIVE_AGENTD_DATA_DIR=<dir> NEWTON_LIVE_AGENTD_PORT=8799 pnpm test
cd src-tauri && NEWTON_DATA_DIR=<dir> NEWTON_PORT=8799 cargo test -- --ignored live_agentd
```
