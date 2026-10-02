# Newton — Specification

Frozen product spec. Change only with an explicit decision recorded in STATUS.md.

## Product goal

A macOS research agent for numerical-methods work. It watches the literature, turns
papers into structured research cards, proposes experiments against the user's own
benchmarks, runs them on a remote Linux GPU box, and reports evidence
(green / yellow / red / unknown) on whether a method actually helps.

The Mac is the control plane (UI, state, memory, scheduling). Linux is invisible
compute behind SSH. The user never touches a Linux terminal.

## Target user

A researcher or engineer working on PDE solvers / CFD / scientific computing who
has a GPU machine reachable by SSH (a GB10 / DGX Spark, their own Linux box, a lab
server) or a capable Mac (Apple Silicon GPU), and wants validation of new methods
without babysitting jobs.

## Architecture

```text
macOS                                         Remote Linux (GPU)
┌──────────────────────────────┐              ┌──────────────────────────────┐
│ Tauri UI  (later milestone)  │              │ newton-worker             │
│        │ HTTP + bearer token │              │  HTTP on 127.0.0.1:<port>    │
│        ▼                     │  ssh -L      │  ├─ job store (files)        │
│ agentd (Python, FastAPI)     │═════════════▶│  ├─ detached job runner      │
│  ├─ SQLite (state, memory)   │  tunnel      │  ├─ hardware probe           │
│  ├─ scheduler + state machine│              │  └─ artifact packer          │
│  ├─ approvals                │  ssh <argv>  │                              │
│  ├─ runners: local/ssh       │─────────────▶│ bootstrap only (fixed argv)  │
│  ├─ artifacts on disk        │              └──────────────────────────────┘
│  └─ Keychain for secrets     │
└──────────────────────────────┘
```

Key decisions:

1. **One job protocol, many transports.** The worker exposes a small HTTP API.
   `local` runs the worker as a child process on the Mac (CPU, or the Apple GPU via
   MLX); `ssh` reaches the remote worker (GB10 / Linux NVIDIA box) through an
   `ssh -N -L` tunnel. The scheduler only ever talks HTTP. (Brev: out of scope,
   decision 2026-10-01 in STATUS.md.)
2. **SSH is the system OpenSSH client.** It honours `~/.ssh/config`, the agent,
   ProxyJump and NVIDIA Sync aliases. Plain `ssh <argv>` is used only for bootstrap
   (install / start / stop the worker) with fixed argument vectors.
3. **Jobs are structured manifests, never shell strings.** An `ExperimentSpec`
   (benchmark name + typed params) is compiled by agentd into a `JobManifest`.
   Params travel as `params.json` inside the job bundle. The worker executes
   `command` as an argv list with no shell, and only allows the interpreter
   `python` (mapped to its own interpreter).
4. **Both sides are crash-safe.** agentd persists every transition in SQLite and
   reconciles on start. The worker runs each job in a detached wrapper that writes
   `status.json` atomically, so jobs survive worker restarts and tunnel drops.
   Submission is idempotent on `job_id`.
5. **Heavy compute remote, everything else local.** SQLite, git, scheduling, UI,
   reporting stay on the Mac. Inference, OCR, embeddings, simulations, sweeps run
   on the worker.

## Components

| Path | Role |
|---|---|
| `services/agentd` | macOS daemon: API, DB, scheduler, runners, research, reporting |
| `services/worker` | Remote worker: stdlib-only Python ≥ 3.9, HTTP job API |
| `benchmarks/` | Benchmark code shipped inside job bundles |
| `packages/contracts` | JSON Schemas exported from agentd's pydantic models |
| `apps/desktop` | Tauri UI (built after the backend is functional) |

## Non-goals (for the hackathon scope)

- Multi-user / team features, cloud sync.
- Running arbitrary model-generated code or shell commands.
- Creating, stopping or deleting cloud instances without explicit approval.
- Windows / Linux desktop builds.
- Training models. We only run inference and simulations.

## Security constraints

- agentd binds to `127.0.0.1` only and requires a bearer token (generated at first
  start, stored `0600` in the data dir, handed to the UI by the Tauri shell).
- The worker binds to `127.0.0.1` on the remote and requires a bearer token; it is
  only reachable through the SSH tunnel. Worker tokens live in the macOS Keychain.
- No credentials in source code or SQLite. SQLite stores Keychain references only.
- Host keys are verified (`StrictHostKeyChecking=yes`). New hosts go through an
  explicit scan → show fingerprint → trust step; trusted keys go into a
  Newton-managed known_hosts file alongside the user's own.
- Never construct shell commands by interpolating model-generated text.
- Bundle extraction and artifact packing reject absolute paths, `..`, and links.
- Publishing, git pushes, and instance lifecycle actions always require approval.

## Demo story (90 seconds)

1. User sets a goal: "Find higher-order advection schemes that beat first-order
   upwind without losing conservation."
2. A new arXiv paper appears in the inbox; the agent extracts the scheme and
   proposes an experiment: baseline upwind vs candidate on 1D linear advection,
   grid-refinement study on the GPU box.
3. User clicks Approve.
4. Jobs run remotely; progress and logs stream in the Mac UI.
5. Report: observed order of accuracy, L2 error, runtime, conservation, stability.
   Evidence status turns green / yellow / red, with reproducible commands.

## Definition of done (per milestone)

- The milestone's acceptance tests in PLAN.md pass via `scripts/test.sh`.
- `ruff`, `mypy` and the test suite are clean.
- STATUS.md is updated with what was built, verification output and next action.
