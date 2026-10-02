# Newton

A macOS research agent that validates numerical methods on a remote GPU box.
The Mac is the control plane; Linux is invisible compute behind SSH.
See [SPEC.md](SPEC.md), [PLAN.md](PLAN.md), [STATUS.md](STATUS.md).

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
