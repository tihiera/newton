#!/bin/sh
# Developers: run agentd in the foreground with a repo-local data dir (not your real
# one). To use Newton, run scripts/run.sh instead.
#   scripts/dev.sh            → http://127.0.0.1:8765, token in .data/api-token
# `pnpm dev:repo` (apps/desktop) reuses this agentd, or starts its own in .data if none runs.
set -eu
cd "$(dirname "$0")/.."
export NEWTON_DATA_DIR="${NEWTON_DATA_DIR:-$PWD/.data}"
export NEWTON_SECRET_BACKEND="${NEWTON_SECRET_BACKEND:-keychain}"
exec uv run newton-agentd serve --port "${NEWTON_PORT:-8765}" --log-level "${LOG_LEVEL:-info}"
