# Newton

A macOS research agent that validates numerical methods on a remote GPU box.
The Mac is the control plane; Linux is invisible compute behind SSH.
See [SPEC.md](SPEC.md), [PLAN.md](PLAN.md), [STATUS.md](STATUS.md).

## Run Newton on your Mac

Newton runs from this repository: there is no installer to download.

**You need** macOS 14 or newer (Apple Silicon recommended: the Apple GPU and Mac
models need it; Intel Macs run experiments on the CPU), git, and Node.js 22.12 or newer,
or 20.19 or a newer 20.x (https://nodejs.org or `brew install node`). Optional: Rust
(https://rustup.rs) for the desktop window; without it Newton opens in your browser with
the same features. On Apple Silicon `scripts/setup.sh` stops on macOS 13 or older: MLX
has no build for it.

```bash
git clone https://github.com/tihiera/newton.git
cd newton
scripts/setup.sh     # once: installs uv if you agree (asks first), the Python env, the UI packages
scripts/run.sh       # starts Newton: the desktop window, or the browser without Rust
```

`scripts/setup.sh` never uses sudo and never installs Node or Rust for you; it says
what is missing and how to get it, and you can run it again at any time
(`--dry-run` shows what it would do, `--yes` installs uv without asking). It sets the
engine up on Python 3.13 (uv downloads it if this Mac doesn't have it; `NEWTON_PYTHON`
picks another 3.12–3.14), and with Rust it downloads the desktop window's Rust packages
ahead of time. The desktop window's first start compiles it, which takes a few minutes
once. If the window's Rust packages are missing and can't be downloaded,
`scripts/run.sh` opens Newton in the browser instead.

**Your data** (database, reports, the engine's API token and its log
`logs/agentd.log`) lives in `~/Library/Application Support/Newton`; set
`NEWTON_DATA_DIR` to use another folder. Connector tokens go to the macOS Keychain.

**Stopping:** close the window, or press Ctrl-C in the terminal running
`scripts/run.sh`; from another terminal, `scripts/run.sh --stop`.
`scripts/run.sh --status` shows what is running, `scripts/run.sh --browser` uses the
browser even when Rust is installed, and `NEWTON_PORT=8766 scripts/run.sh` moves the
engine off port 8765. The UI uses port 1420; if something else holds it, run.sh says
which program and stops.

## Quick start (backend)

```bash
uv sync                          # Python 3.12+ workspace
scripts/test.sh                  # lint, mypy, contracts, tests
scripts/dev.sh                   # agentd on http://127.0.0.1:8765 (data in .data/)
uv run python scripts/demo.py    # goal → experiment → approve → run → report
```

API token: `.data/api-token` (dev) or `~/Library/Application Support/Newton/api-token`.
Every endpoint except `/health` requires `Authorization: Bearer <token>`.

## Adding a GPU host (GB10 / DGX Spark or any Linux NVIDIA box)

Any SSH route from this Mac works: an NVIDIA Sync pairing, an alias in
`~/.ssh/config`, or `user@host`. Newton never prompts and never weakens host-key
checking; a host it hasn't seen yet needs one fingerprint approval.

```bash
TOKEN=$(cat .data/api-token); H="Authorization: Bearer $TOKEN"; API=http://127.0.0.1:8765
curl -sH "$H" $API/ssh/hosts                                   # pick an alias (Sync hosts tagged)
curl -sH "$H" -H 'Content-Type: application/json' $API/hosts -d '{"name": "spark", "ssh_target": "SparkLAN"}'
curl -sH "$H" -X POST $API/hosts/<id>/connect                  # 409 + fingerprints if not trusted yet
curl -sH "$H" -H 'Content-Type: application/json' $API/hosts/<id>/hostkeys/trust \
  -d '{"fingerprints": ["SHA256:..."]}'                         # compare with the Spark first
curl -sH "$H" -X POST $API/hosts/<id>/connect                  # installs, starts, checks, selftest
```

On the host Newton lives in `~/.newton` (0700): its own venv (never the system
Python), the worker on a Unix socket only you can reach, jobs and logs.
If setup can't work, connect says exactly why (e.g. `venv_unavailable`:
`sudo apt install python3-venv python3-pip`).

## Layout

| Path | What |
|---|---|
| `services/agentd` | macOS daemon: API, SQLite, scheduler, runners, evaluation, reports |
| `services/worker` | Remote worker (stdlib, Python ≥ 3.9) + `bootstrap.sh` (venv, deps, start) |
| `benchmarks/advection` | 1D linear advection: upwind / Lax–Wendroff / MUSCL |
| `packages/contracts` | JSON Schemas generated from `newton_agentd/contracts.py` |
| `apps/desktop` | Tauri UI (milestone U1) |
