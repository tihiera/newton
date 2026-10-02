# Agent instructions

Read SPEC.md, PLAN.md and STATUS.md before changing code.

- Implement only the current milestone (see STATUS.md). Do not start the next one
  until the current milestone's acceptance tests pass.
- Backend first: the UI consumes the agentd HTTP API and must not contain
  business logic.
- Keep the macOS control plane (`services/agentd`) independent from the remote
  worker (`services/worker`). They share only the JSON contracts. The worker stays
  stdlib-only and compatible with Python 3.9.
- Never place credentials in source code or SQLite. Use `newton_agentd.secrets`.
- Never construct shell commands by interpolating model-generated text. Remote
  execution goes through a `JobManifest` compiled from a typed `ExperimentSpec`;
  SSH bootstrap uses fixed argv lists.
- All remote experiments use a structured `ExperimentSpec`.
- Schema changes: add a new file in `services/agentd/newton_agentd/storage/migrations/`,
  never edit an applied migration. After changing contract models run
  `uv run python scripts/export_schemas.py`.
- Run `scripts/test.sh` before finishing. Update STATUS.md after each completed
  unit of work.
