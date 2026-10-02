#!/bin/sh
# Run agentd in the foreground with a repo-local data dir (not your real one).
#   scripts/dev.sh            → http://127.0.0.1:8765, token in .data/api-token
set -eu
cd "$(dirname "$0")/.."
export NEWTON_DATA_DIR="${NEWTON_DATA_DIR:-$PWD/.data}"
export NEWTON_SECRET_BACKEND="${NEWTON_SECRET_BACKEND:-keychain}"
exec uv run newton-agentd serve --port "${NEWTON_PORT:-8765}" --log-level "${LOG_LEVEL:-info}"
