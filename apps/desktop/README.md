# Newton desktop (Tauri v2)

macOS shell over the agentd HTTP API. The UI only shows what agentd returns. All
product logic stays in `services/agentd`.

For people who just want to use Newton: from the repository root, `scripts/setup.sh`
then `scripts/run.sh` (see the root README). This page is for working on the app.

## Run (development)

```sh
pnpm install
pnpm dev:repo      # tauri dev with the repo-local data dir <repo>/.data
pnpm dev           # tauri dev with the default data dir (~/Library/Application Support/Newton)
pnpm dev:web       # the UI in a browser (Vite on :1420), against a running agentd
```

`tauri dev` starts agentd itself (`uv run --project <repo> --frozen newton-agentd serve`)
when none is running for that data dir, reuses one that is (for example
`scripts/dev.sh`), and stops only the agentd it started when the window closes. Its log
is `<data_dir>/logs/agentd.log`. Newton isn't distributed as a packaged app: people run
it from a clone.

A port that accepts but answers `/health` slowly (a busy agentd) is probed again, not
taken for another program: the port only counts as taken after several whole non-agentd
answers, or about 20 s of silence, and never while the agentd the shell just spawned
still runs. When the engine fails, the shell writes the reason at the end of
`agentd.log` and waits for **Restart engine** (it doesn't retry by itself).

| Script                       | What it does                                                       |
| ---------------------------- | ------------------------------------------------------------------ |
| `pnpm dev` / `pnpm dev:repo` | `tauri dev` (Vite on :1420 plus the native window, starts agentd)  |
| `pnpm dev:web`               | Vite only, for a browser (`scripts/run.sh --browser` wraps this)   |
| `pnpm build`                 | Type-check and build the frontend into `dist/`                     |
| `pnpm typecheck`             | `tsc` for `src/` and `vite.config.ts`                              |
| `pnpm lint` / `pnpm format`  | ESLint (with React's hook rules) / Prettier                        |
| `pnpm test`                  | vitest (API client and view helpers, with fetch and invoke mocked) |

Rust tests: `cd src-tauri && cargo test` (`cargo test -- --ignored live_engine` runs the
shell's engine start/reuse/stop against a real agentd).

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
  `token_missing | config | engine_starting | engine_failed | unreachable |
unauthorized | http`. `src/api/types.ts` holds the hand-written response types. `packages/contracts` has only
  the request schemas.
- Security:
  - The CSP (`src-tauri/tauri.conf.json`) allows `connect-src` only to Tauri IPC and
    `http://127.0.0.1:*`.
  - The command is gated by the capability ACL (`src-tauri/capabilities/default.json`
    allows `allow-agentd-connection`).
  - agentd's CORS list already includes `tauri://localhost` (the app window) and
    `http://localhost:1420` (`tauri dev`).
- Apps launched from Finder don't inherit shell env vars, so they use the default
  data dir and port.

Live check against a running agentd (e.g.
`NEWTON_DATA_DIR=$(mktemp -d) uv run newton-agentd serve --port 8799`):

```sh
NEWTON_LIVE_AGENTD_DATA_DIR=<dir> NEWTON_LIVE_AGENTD_PORT=8799 pnpm test
cd src-tauri && NEWTON_DATA_DIR=<dir> NEWTON_PORT=8799 cargo test -- --ignored live_agentd
```
