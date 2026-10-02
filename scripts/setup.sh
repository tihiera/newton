#!/bin/sh
# Set Newton up on this Mac, from its git clone: the Python environment (uv) and the
# window's packages (pnpm). Safe to run again: it only adds what is missing.
#   scripts/setup.sh             asks before installing uv
#   scripts/setup.sh --yes       installs uv without asking
#   scripts/setup.sh --dry-run   says what it would do and changes nothing
# Never uses sudo and never installs Node or Rust: it says how instead.
set -eu
cd "$(dirname "$0")/.."
REPO=$(pwd -P)

YES=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --yes | -y) YES=1 ;;
    --dry-run | -n) DRY=1 ;;
    -h | --help)
      sed -n '2,8p' "$REPO/scripts/setup.sh" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "setup.sh: unknown option $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

UV_INSTALL_URL="https://astral.sh/uv/install.sh"
# The Python the engine runs on. Without a pin uv takes the newest one it finds or can
# download, and uv.lock has no build of some packages (MLX, pydantic-core) for a Python
# newer than 3.14. uv downloads this one when the Mac doesn't have it.
PYTHON_VERSION=${NEWTON_PYTHON:-3.13}
TAURI_MANIFEST=apps/desktop/src-tauri/Cargo.toml
# Node.js: what the locked vite accepts (engines "^20.19.0 || >=22.12.0").
NODE_NEED="20.19 or newer 20.x, or 22.12 or newer"
# corepack's pnpm shim otherwise asks (on the terminal) before downloading pnpm, and
# the first `pnpm --version` below would wait for an answer the user never sees.
COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export COREPACK_ENABLE_DOWNLOAD_PROMPT

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
ok() { printf '  ok %s\n' "$*"; }
# Prints a command, then runs it (not with --dry-run).
run() {
  printf '  $ %s\n' "$*"
  if [ "$DRY" = 1 ]; then return 0; fi
  "$@"
}

# uv: PATH, then where its installers put it (the same places the desktop shell looks).
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$c" ]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

find_cargo() {
  if command -v cargo >/dev/null 2>&1; then
    command -v cargo
  elif [ -x "$HOME/.cargo/bin/cargo" ]; then
    printf '%s\n' "$HOME/.cargo/bin/cargo"
  else
    return 1
  fi
}

PY_READY=no
UI_READY=no
WINDOW_READY=no
APPLE_GPU=no
TODO=""
todo() { TODO="$TODO
  - $*"; }

step "This Mac"
OS=$(uname -s)
if [ "$OS" != Darwin ]; then
  say "Newton runs on macOS; this is $OS. Stopping here."
  exit 1
fi
MACOS=$(sw_vers -productVersion 2>/dev/null || echo 0)
MACOS_MAJOR=${MACOS%%.*}
ARCH=$(uname -m)
ok "macOS $MACOS on $ARCH"
if [ "$ARCH" = x86_64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
  warn "This terminal runs under Rosetta: open a native (arm64) terminal so Newton gets the Apple GPU."
fi
case "$ARCH" in
  arm64)
    # MLX (locked in uv.lock for every Apple Silicon Mac) has no build for macOS 13
    # or older: `uv sync` would fail, so stop before changing anything.
    if [ "$MACOS_MAJOR" -ge 14 ] 2>/dev/null; then
      APPLE_GPU=yes
    else
      say "Newton needs macOS 14 or newer on Apple Silicon (MLX has no build for macOS $MACOS_MAJOR): update macOS, then run scripts/setup.sh again."
      exit 1
    fi
    ;;
  x86_64)
    warn "Intel Mac: experiments run on the CPU. The Apple GPU (Metal/MLX) and Mac models need Apple Silicon."
    ;;
  *)
    say "Unsupported processor: $ARCH. Stopping here."
    exit 1
    ;;
esac

if command -v git >/dev/null 2>&1; then
  ok "$(git --version)"
else
  warn "git is missing: install the Xcode Command Line Tools with: xcode-select --install"
  todo "Install git: xcode-select --install"
fi

step "Python environment (uv)"
UV=$(find_uv || true)
if [ -z "$UV" ]; then
  say "Newton runs its engine with uv (https://docs.astral.sh/uv/), which isn't installed."
  say "To install it, this runs the official installer (no sudo; it goes to ~/.local/bin):"
  say "  curl -LsSf $UV_INSTALL_URL | sh"
  answer=n
  if [ "$YES" = 1 ]; then
    answer=y
  elif [ "$DRY" = 1 ]; then
    answer=n
  elif [ -t 0 ]; then
    printf 'Install uv now? [y/N] '
    read -r answer || answer=n
  fi
  case "$answer" in
    y | Y | yes | YES)
      if [ "$DRY" = 1 ]; then
        say "  (dry run: not installing)"
      else
        curl -LsSf "$UV_INSTALL_URL" | sh
        UV=$(find_uv || true)
      fi
      ;;
    *) say "  Not installing uv." ;;
  esac
fi
if [ -n "$UV" ]; then
  ok "$("$UV" --version) ($UV)"
  # --all-packages: the `mac` extra belongs to the agentd package, not the workspace root.
  if [ "$ARCH" = arm64 ]; then
    run "$UV" sync --frozen --python "$PYTHON_VERSION" --all-packages --extra mac
  else
    run "$UV" sync --frozen --python "$PYTHON_VERSION"
  fi
  PY_READY=yes
  if [ -x "$REPO/.venv/bin/python" ]; then
    PY_READY="yes ($("$REPO/.venv/bin/python" --version 2>&1))"
  fi
else
  todo "Install uv: scripts/setup.sh --yes (or see https://docs.astral.sh/uv/getting-started/installation/)"
fi

# Whether Node.js version $1 (v<major>.<minor>.<patch>) is in vite's engines range.
node_supported() {
  v=${1#v}
  major=${v%%.*}
  rest=${v#*.}
  minor=${rest%%.*}
  case "$major$minor" in '' | *[!0-9]*) return 1 ;; esac
  case "$major" in
    20) [ "$minor" -ge 19 ] ;;
    22) [ "$minor" -ge 12 ] ;;
    *) [ "$major" -ge 23 ] ;;
  esac
}

step "Window packages (Node.js, pnpm)"
NODE_OK=no
if command -v node >/dev/null 2>&1; then
  NODE_VERSION=$(node --version 2>/dev/null || echo unknown)
  if node_supported "$NODE_VERSION"; then
    NODE_OK=yes
    ok "Node.js $NODE_VERSION"
  else
    warn "Node.js $NODE_VERSION can't run Newton's UI: it needs Node.js $NODE_NEED (https://nodejs.org, or: brew install node)."
    todo "Update Node.js to $NODE_NEED: https://nodejs.org (or: brew install node)"
  fi
else
  warn "Node.js isn't installed. Install Node.js $NODE_NEED from https://nodejs.org (or: brew install node), then run scripts/setup.sh again."
  todo "Install Node.js $NODE_NEED: https://nodejs.org (or: brew install node)"
fi
PNPM_LOCAL=no
if [ "$NODE_OK" = yes ]; then
  if ! command -v pnpm >/dev/null 2>&1; then
    if command -v corepack >/dev/null 2>&1; then
      say "pnpm isn't installed; Node's corepack provides it (it downloads pnpm on first use):"
      if ! run corepack enable pnpm; then
        # Node installed system-wide: put the shim in the user's folder instead.
        run corepack enable --install-directory "$HOME/.local/bin" pnpm || true
        case ":$PATH:" in
          *":$HOME/.local/bin:"*) ;;
          *)
            PATH="$HOME/.local/bin:$PATH"
            PNPM_LOCAL=yes
            ;;
        esac
      fi
    else
      warn "pnpm isn't installed and this Node.js has no corepack. Install pnpm with: npm install --global pnpm (or: brew install pnpm)"
    fi
  fi
  if command -v pnpm >/dev/null 2>&1 || [ "$DRY" = 1 ]; then
    if command -v pnpm >/dev/null 2>&1; then ok "pnpm $(pnpm --version 2>/dev/null)"; fi
    if [ "$PNPM_LOCAL" = yes ]; then
      warn "pnpm is in ~/.local/bin, which isn't on your PATH: scripts/run.sh finds it there; to use pnpm yourself, add ~/.local/bin to your PATH."
    fi
    run pnpm --dir apps/desktop install --frozen-lockfile
    UI_READY=yes
  else
    todo "Install pnpm: npm install --global pnpm (or: brew install pnpm)"
  fi
fi

step "Desktop window (Rust)"
CARGO=$(find_cargo || true)
if [ -n "$CARGO" ]; then
  ok "$("$CARGO" --version)"
  if ! xcode-select -p >/dev/null 2>&1; then
    warn "Rust needs the Xcode Command Line Tools to build the window: xcode-select --install"
    todo "Install the Xcode Command Line Tools: xcode-select --install"
  elif [ "$UI_READY" = yes ]; then
    # Download the window's Rust packages now, while online: its first build (on the
    # first scripts/run.sh) then works offline too.
    if run "$CARGO" fetch --manifest-path "$TAURI_MANIFEST"; then
      WINDOW_READY=yes
    else
      warn "Couldn't download the desktop window's Rust packages (is this Mac offline?). Newton opens in the browser until scripts/setup.sh runs again online."
    fi
  fi
else
  say "  Rust isn't installed. The desktop window needs it (https://rustup.rs);"
  say "  without it Newton opens in your browser instead, with the same features."
fi

step "Summary"
say "  Engine (Python, uv):                $PY_READY"
say "  Newton in the browser:              $UI_READY"
say "  Desktop window (Rust):              $WINDOW_READY"
say "  Apple GPU (Metal/MLX) on this Mac: $APPLE_GPU"
if [ "$DRY" = 1 ]; then
  say ""
  say "Dry run: nothing was installed or changed."
fi
if [ -n "$TODO" ]; then
  say ""
  say "Still to do:$TODO"
  say "then run scripts/setup.sh again."
  [ "$DRY" = 1 ] || exit 1
  exit 0
fi
say ""
if [ "$WINDOW_READY" = yes ]; then
  say "Ready. Start Newton with: scripts/run.sh   (the browser instead: scripts/run.sh --browser)"
  say "The desktop window's first start compiles it: a few minutes, once."
else
  say "Ready. Start Newton with: scripts/run.sh   (it opens in your browser)"
fi
