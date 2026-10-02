#!/bin/sh
# Start Newton from this clone: its engine (agentd), then the desktop window, or the
# browser UI when Rust isn't installed. Ctrl-C (or closing the window) stops both.
#   scripts/run.sh             the desktop window if Rust (cargo) is installed, else the browser
#   scripts/run.sh --browser   the browser UI at http://localhost:1420
#   scripts/run.sh --no-open   (browser) don't open a browser tab
#   scripts/run.sh --status    what is running for this data folder
#   scripts/run.sh --stop      stop a Newton that scripts/run.sh started (from another terminal)
# Data: $NEWTON_DATA_DIR, else ~/Library/Application Support/Newton (your real data;
# scripts/dev.sh uses the repo's .data instead). Engine port: $NEWTON_PORT, else 8765.
# Engine log: <data folder>/logs/agentd.log. Run scripts/setup.sh once first.
set -eu
# NEWTON_DATA_DIR is relative to where run.sh was started, so remember that first.
CALLER_PWD=$(pwd)
cd "$(dirname "$0")/.."
REPO=$(pwd -P)

MODE=auto
OPEN=1
ACTION=run
for arg in "$@"; do
  case "$arg" in
    --browser) MODE=browser ;;
    --no-open) OPEN=0 ;;
    --status) ACTION=status ;;
    --stop) ACTION=stop ;;
    -h | --help)
      sed -n '2,11p' "$REPO/scripts/run.sh" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "run.sh: unknown option $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

if [ -n "${NEWTON_DATA_DIR:-}" ]; then
  # An absolute path for agentd, the PID files and the window (whose working folder is
  # apps/desktop/src-tauri): expand a leading ~ (also when it was quoted), and resolve a
  # relative path against the folder run.sh was started from.
  DATA_DIR=$NEWTON_DATA_DIR
  case "$DATA_DIR" in
    /*) ;;
    '~') DATA_DIR=$HOME ;;
    '~/'*) DATA_DIR=$HOME/${DATA_DIR#"~/"} ;;
    *) DATA_DIR=$CALLER_PWD/$DATA_DIR ;;
  esac
elif [ "$(uname -s)" = Darwin ]; then
  DATA_DIR="$HOME/Library/Application Support/Newton"
else
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/newton"
fi
PORT=${NEWTON_PORT:-8765}
UI_PORT=1420
ENGINE_URL="http://127.0.0.1:$PORT"
UI_URL="http://localhost:$UI_PORT"
LOG="$DATA_DIR/logs/agentd.log"
RUN_PID_FILE="$DATA_DIR/run.pid"
AGENTD_PID_FILE="$DATA_DIR/run-agentd.pid"
START_TIMEOUT=${NEWTON_START_TIMEOUT:-90}
PY="$REPO/.venv/bin/python"

say() { printf '%s\n' "$*"; }
die() {
  printf '%s\n' "$*" >&2
  exit 1
}

case "$PORT" in
  '' | *[!0-9]*) die "NEWTON_PORT is not a port number: $PORT" ;;
esac

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

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
read_pid() { if [ -f "$1" ]; then sed -n '1p' "$1" 2>/dev/null; fi; }
# PIDs listening on a local TCP port (needs lsof, which macOS has).
listeners() { command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u; }
describe_pids() { for p in $1; do ps -p "$p" -o pid= -o command= 2>/dev/null | cut -c1-160; done; }

# What answers on the engine port: "free", "ours" (an agentd with this data folder),
# "other <data folder>" (another agentd) or "foreign" (another program).
# Loopback requests never go through a proxy: curl would otherwise send them to the
# http_proxy/all_proxy many company Macs set, and the engine would never answer.
probe() {
  body=$(curl -s --noproxy '*' --max-time 2 "$ENGINE_URL/health" 2>/dev/null) || body=""
  if [ -z "$body" ]; then
    if [ -n "$(listeners "$PORT")" ]; then echo foreign; else echo free; fi
    return 0
  fi
  # /health is agentd's: {"status": ..., "db": {"path": "<data dir>/newton.db"}, ...}.
  db=$(printf '%s' "$body" | "$PY" -c 'import json,sys
try:
    h = json.load(sys.stdin)
    print(h["db"]["path"] if "status" in h else "")
except Exception:
    print("")' 2>/dev/null) || db=""
  if [ -z "$db" ]; then
    echo foreign
    return 0
  fi
  dir=$(dirname "$db")
  if [ "$(cd "$dir" 2>/dev/null && pwd -P)" = "$(cd "$DATA_DIR" 2>/dev/null && pwd -P)" ]; then
    echo ours
  else
    echo "other $dir"
  fi
}

status() {
  say "Data folder: $DATA_DIR"
  run_pid=$(read_pid "$RUN_PID_FILE")
  if alive "$run_pid"; then
    say "scripts/run.sh: running (PID $run_pid)"
  else
    say "scripts/run.sh: not running"
  fi
  if [ ! -x "$PY" ]; then
    say "Engine: unknown (run scripts/setup.sh first)"
    return 0
  fi
  state=$(probe)
  case "$state" in
    ours)
      agentd_pid=$(read_pid "$AGENTD_PID_FILE")
      if alive "$agentd_pid"; then
        say "Engine: running on $ENGINE_URL (started by scripts/run.sh, PID $agentd_pid)"
      else
        say "Engine: running on $ENGINE_URL (started by the desktop window or scripts/dev.sh)"
      fi
      ;;
    free) say "Engine: not running (port $PORT is free)" ;;
    foreign) say "Engine: port $PORT is taken by another program: $(describe_pids "$(listeners "$PORT")")" ;;
    *) say "Engine: another Newton engine is on port $PORT, with its data in ${state#other }" ;;
  esac
  ui=$(listeners "$UI_PORT" || true)
  if [ -n "$ui" ]; then say "UI port $UI_PORT: $(describe_pids "$ui")"; else say "UI port $UI_PORT: free"; fi
  say "scripts/run.sh opens: $UI_DESC"
}

# SIGTERM, then SIGKILL after $2 seconds.
stop_pid() {
  pid=$1
  alive "$pid" || return 0
  kill -TERM "$pid" 2>/dev/null || return 0
  i=0
  while alive "$pid" && [ "$i" -lt $(($2 * 10)) ]; do
    sleep 0.1
    i=$((i + 1))
  done
  if alive "$pid"; then kill -KILL "$pid" 2>/dev/null || true; fi
}

stop() {
  run_pid=$(read_pid "$RUN_PID_FILE")
  if alive "$run_pid" && ps -p "$run_pid" -o command= 2>/dev/null | grep -q 'run\.sh'; then
    say "Stopping Newton (scripts/run.sh, PID $run_pid)..."
    stop_pid "$run_pid" 30
    say "Stopped."
    return 0
  fi
  agentd_pid=$(read_pid "$AGENTD_PID_FILE")
  if alive "$agentd_pid" && ps -p "$agentd_pid" -o command= 2>/dev/null | grep -q 'newton-agentd'; then
    say "Stopping Newton's engine (PID $agentd_pid)..."
    stop_pid "$agentd_pid" 15
    rm -f "$AGENTD_PID_FILE"
    say "Stopped."
    return 0
  fi
  rm -f "$RUN_PID_FILE" "$AGENTD_PID_FILE"
  say "No Newton started by scripts/run.sh is running for $DATA_DIR."
  if [ -x "$PY" ] && [ "$(probe)" = ours ]; then
    say "An engine for this data folder answers on $ENGINE_URL: it was started by the desktop window or scripts/dev.sh; quit it there."
  fi
}

# The desktop window needs cargo (Rust) and pnpm: on PATH, or where rustup and
# scripts/setup.sh's corepack fallback put them.
CARGO=""
if command -v cargo >/dev/null 2>&1; then
  CARGO=$(command -v cargo)
elif [ -x "$HOME/.cargo/bin/cargo" ]; then
  CARGO="$HOME/.cargo/bin/cargo"
  PATH="$HOME/.cargo/bin:$PATH"
  export PATH
fi
PNPM=""
if command -v pnpm >/dev/null 2>&1; then
  PNPM=$(command -v pnpm)
elif [ -x "$HOME/.local/bin/pnpm" ]; then
  PNPM="$HOME/.local/bin/pnpm"
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi
WHY_BROWSER=""
if [ "$MODE" = auto ]; then
  if [ -z "$CARGO" ]; then
    MODE=browser
    WHY_BROWSER="Rust (cargo) isn't installed"
  elif [ -z "$PNPM" ]; then
    MODE=browser
    WHY_BROWSER="pnpm isn't installed"
  else
    MODE=window
  fi
fi
case "$MODE" in
  window) UI_DESC="the desktop window" ;;
  *) UI_DESC="the browser UI${WHY_BROWSER:+ (no desktop window: $WHY_BROWSER)}" ;;
esac

case "$ACTION" in
  status)
    status
    exit 0
    ;;
  stop)
    stop
    exit 0
    ;;
esac

# --- run ---------------------------------------------------------------------------

UV=$(find_uv || true)
[ -n "$UV" ] || die "uv isn't installed: run scripts/setup.sh first."
[ -x "$PY" ] || die "Newton's Python environment isn't set up: run scripts/setup.sh first."
[ -x apps/desktop/node_modules/.bin/vite ] || die "The window's packages aren't installed: run scripts/setup.sh first."
command -v curl >/dev/null 2>&1 || die "curl is missing (it comes with macOS)."

if [ -n "$WHY_BROWSER" ] && [ -n "$CARGO" ]; then
  say "The desktop window needs pnpm, which isn't installed: Newton opens in the browser instead (scripts/setup.sh installs pnpm)."
fi

old=$(read_pid "$RUN_PID_FILE")
if alive "$old" && ps -p "$old" -o command= 2>/dev/null | grep -q 'run\.sh'; then
  die "Newton is already running for this data folder (scripts/run.sh, PID $old). Stop it with: scripts/run.sh --stop"
fi

# The UI's port first: nothing is started when it can't be used. Not ours to stop.
held=$(listeners "$UI_PORT" || true)
if [ -n "$held" ]; then
  say "Port $UI_PORT, which Newton's UI needs, is in use by:" >&2
  describe_pids "$held" | sed 's/^/  /' >&2
  die "Quit that program (or stop its dev server), then run scripts/run.sh again."
fi

mkdir -p "$DATA_DIR/logs"
NEWTON_DATA_DIR=$DATA_DIR
NEWTON_PORT=$PORT
export NEWTON_DATA_DIR NEWTON_PORT
unset NEWTON_PACKAGED || true

AGENTD_PID=""
UI_PID=""
STOPPING=0
cleanup() {
  # Runs to the end whatever fails, e.g. writing to a terminal that was closed (set -e
  # would otherwise leave the engine running).
  set +e
  [ "$STOPPING" = 0 ] || return 0
  STOPPING=1
  trap '' INT TERM HUP
  if alive "$UI_PID"; then
    # The UI runs in its own process group (vite, or tauri dev with cargo and the app).
    # `kill -s SIG -- -PGID` is the form both bash and dash accept.
    kill -s INT -- "-$UI_PID" 2>/dev/null || kill -s INT "$UI_PID" 2>/dev/null
    i=0
    while alive "$UI_PID" && [ "$i" -lt 50 ]; do
      sleep 0.1
      i=$((i + 1))
    done
    if alive "$UI_PID"; then kill -s TERM -- "-$UI_PID" 2>/dev/null || kill -s TERM "$UI_PID" 2>/dev/null; fi
  fi
  if alive "$AGENTD_PID"; then
    # In a subshell: when the terminal is gone, bash keeps the unwritten text and
    # would print it into the next $(...) below.
    (say "Stopping Newton's engine...") 2>/dev/null
    # uv passes SIGTERM on to agentd, which shuts down cleanly.
    stop_pid "$AGENTD_PID" 15
  fi
  rm -f "$AGENTD_PID_FILE"
  if [ "$(read_pid "$RUN_PID_FILE")" = "$$" ]; then rm -f "$RUN_PID_FILE"; fi
}
# Each signal handler first ignores further signals: closing a terminal can send SIGHUP
# twice, and a second `exit` while the EXIT trap runs would cut the cleanup short.
on_signal() {
  trap '' INT TERM HUP
  exit "$1"
}
trap cleanup EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
# The terminal was closed: dash runs the EXIT trap only through a HUP trap.
trap 'on_signal 129' HUP
echo "$$" >"$RUN_PID_FILE"

# Replaces the calling subshell (always `own_group ... &`, so $! is the command's PID)
# with a command in a process group of its own, with SIGINT back to its default: the
# terminal's Ctrl-C reaches only this script, which then stops everything in order.
own_group() {
  exec "$PY" -c 'import os, signal, sys
os.setpgrp()
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
}

tail_log() {
  if [ -f "$LOG" ]; then
    say "The end of its log ($LOG):" >&2
    tail -n 20 "$LOG" | sed 's/^/  /' >&2
  fi
}

state=$(probe)
case "$state" in
  ours)
    say "Newton's engine is already running on $ENGINE_URL for $DATA_DIR: using it (it keeps running afterwards)."
    ;;
  free)
    if [ -f "$LOG" ]; then mv -f "$LOG" "$LOG.1"; fi
    say "Starting Newton's engine on $ENGINE_URL (data: $DATA_DIR)..."
    own_group "$UV" run --project "$REPO" --frozen newton-agentd serve \
      --port "$PORT" --data-dir "$DATA_DIR" </dev/null >"$LOG" 2>&1 &
    AGENTD_PID=$!
    echo "$AGENTD_PID" >"$AGENTD_PID_FILE"
    waited=0
    while :; do
      if ! alive "$AGENTD_PID"; then
        say "Newton's engine stopped while starting." >&2
        tail_log
        exit 1
      fi
      [ "$(probe)" = ours ] && break
      if [ "$waited" -ge $((START_TIMEOUT * 2)) ]; then
        say "Newton's engine didn't answer within $START_TIMEOUT s." >&2
        tail_log
        exit 1
      fi
      sleep 0.5
      waited=$((waited + 1))
    done
    say "Engine ready. Its log: $LOG"
    ;;
  foreign)
    say "Port $PORT is taken by another program:" >&2
    describe_pids "$(listeners "$PORT")" | sed 's/^/  /' >&2
    die "Quit it, or choose another port: NEWTON_PORT=8766 scripts/run.sh"
    ;;
  *)
    die "Another Newton engine already listens on port $PORT, with its data in ${state#other }. Stop it (scripts/run.sh --stop with that NEWTON_DATA_DIR, or quit it), or choose another port: NEWTON_PORT=8766 scripts/run.sh"
    ;;
esac

# The window compiles from Rust packages that scripts/setup.sh downloads. When they
# are missing and can't be downloaded now (this Mac is offline), `tauri dev` would fail
# and take the engine down with it: open the browser UI, which needs nothing more.
TAURI_MANIFEST=apps/desktop/src-tauri/Cargo.toml
if [ "$MODE" = window ] && ! "$CARGO" fetch --offline --manifest-path "$TAURI_MANIFEST" >/dev/null 2>&1; then
  say "Downloading the desktop window's Rust packages (once)..."
  if ! "$CARGO" fetch --manifest-path "$TAURI_MANIFEST"; then
    MODE=browser
    say "Couldn't download them (is this Mac offline?): Newton opens in the browser instead."
    say "Once online, scripts/setup.sh downloads them for the desktop window."
  fi
fi

if [ "$MODE" = window ]; then
  say "Opening the desktop window (its first start compiles it: a few minutes, once)."
  say "Close the window or press Ctrl-C to quit Newton."
  # The shell finds this engine (same NEWTON_DATA_DIR and NEWTON_PORT) and reuses it.
  own_group "$PNPM" --dir apps/desktop tauri dev </dev/null &
  UI_PID=$!
else
  if [ -n "${NEWTON_API_TOKEN:-}" ]; then
    TOKEN=$NEWTON_API_TOKEN
  else
    TOKEN=$(cat "$DATA_DIR/api-token" 2>/dev/null) || die "Can't read the engine's token in $DATA_DIR/api-token."
  fi
  say "Starting the browser UI on $UI_URL ..."
  # The token goes to this process only, through its environment: never into a file.
  (
    cd apps/desktop
    VITE_AGENTD_URL=$ENGINE_URL
    VITE_AGENTD_TOKEN=$TOKEN
    export VITE_AGENTD_URL VITE_AGENTD_TOKEN
    own_group ./node_modules/.bin/vite --port "$UI_PORT" --strictPort
  ) </dev/null &
  UI_PID=$!
  TOKEN=""
  waited=0
  until curl -s --noproxy '*' -o /dev/null --max-time 1 "$UI_URL/"; do
    alive "$UI_PID" || die "The browser UI didn't start (see the messages above)."
    [ "$waited" -lt 60 ] || die "The browser UI didn't answer on $UI_URL within 30 s."
    sleep 0.5
    waited=$((waited + 1))
  done
  say "Newton is at $UI_URL  (Ctrl-C here quits it)."
  if [ "$OPEN" = 1 ] && command -v open >/dev/null 2>&1; then open "$UI_URL" || true; fi
fi

# `wait` returns early on Ctrl-C or --stop (SIGTERM): the traps then stop everything.
status=0
wait "$UI_PID" || status=$?
UI_PID=""
if [ "$MODE" = window ] && [ "$status" -ne 0 ]; then
  say "The desktop window stopped with an error (see the messages above). Newton also runs in your browser: scripts/run.sh --browser" >&2
fi
exit "$status"
