#!/bin/sh
# Lint, typecheck, contract drift check and tests. Extra args go to pytest,
# e.g. `scripts/test.sh -m live` with NEWTON_TEST_SSH_HOST=<alias>. Tests run in
# parallel (pytest-xdist): about 1m40s instead of 9 minutes serially.
set -eu
cd "$(dirname "$0")/.."

echo "==> ruff"
uv run ruff check .
uv run ruff format --check .
echo "==> mypy"
uv run mypy
echo "==> contracts"
uv run python scripts/export_schemas.py --check
echo "==> bootstrap.sh syntax"
sh -n services/worker/bootstrap.sh
if command -v dash >/dev/null 2>&1; then dash -n services/worker/bootstrap.sh; fi
# Desktop shell (apps/desktop): only where its dependencies are installed
# (`pnpm install` there); the Rust tests only once it has been built.
if [ -d apps/desktop/node_modules ] && command -v pnpm >/dev/null 2>&1; then
  echo "==> desktop (format, lint, typecheck, tests)"
  (cd apps/desktop && pnpm -s format:check && pnpm -s lint && pnpm -s typecheck && pnpm -s test)
  if [ -d apps/desktop/src-tauri/target ] && command -v cargo >/dev/null 2>&1; then
    (cd apps/desktop/src-tauri && cargo test -q)
  fi
fi
# Notion OAuth broker (services/notion-broker): dependency-free, needs only node.
if command -v node >/dev/null 2>&1; then
  echo "==> notion-broker (node --test)"
  (cd services/notion-broker && node --test)
fi
echo "==> pytest (parallel on all cores; pass -n 0 for a serial run)"
uv run pytest -p no:warnings -n auto "$@"
