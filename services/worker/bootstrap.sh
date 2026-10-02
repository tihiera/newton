#!/bin/sh
# Install and (re)start the Newton worker on a host (Linux; macOS works too).
#
# agentd uploads the worker source into a staging dir $FP_HOME/src.stage-<hex>
# and its token to $FP_HOME/tokens/<id>, then runs the staged copy of this script over
# SSH with fixed arguments. Everything that can fail runs before the running
# worker is touched: on any error the host keeps its previous src, venv and worker.
# The last line of stdout is the worker status JSON; diagnostics go to stderr.
# The worker serves on the Unix socket $FP_HOME/worker.sock (dir is 0700), which
# agentd reaches with OpenSSH stream-local forwarding: other users on a shared
# machine can neither connect to it nor squat it.
#
# Packages go into $FP_HOME/venv, never into a system Python (PEP 668 hosts such
# as Ubuntu 24.04 / DGX OS refuse that, and we never --break-system-packages).
#   venv:      python -m venv  →  python -m venv --without-pip  →  uv venv
#   installer: venv pip  →  system pip --python (pip >= 22.3)  →  system pip --target  →  uv pip
# A replacement venv is built in venv.new and swapped in only once numpy works.
#
# GPU support is a separate run, `--gpu-only auto|cuda`, made by agentd once the
# worker serves (so a slow multi-GB download never holds up or fails the worker
# itself): CuPy matching the NVIDIA driver goes into the same venv through the
# same installer chain, then a smoke test runs in a child process. The outcome is
# recorded in $FP_HOME/gpu.json (see newton_worker/gpu.py) and printed as the last
# line. "auto" retries a failed attempt at most daily; "cuda" (the user asked)
# always retries, and exits 75 while jobs are running.
#
# Exit codes (outside the shell's own 1/2/126/127; mapped in agentd runners/ssh.py):
#   64 bad arguments / install location   65 token missing       66 python missing or < 3.9
#   67 worker did not start or stop       68 numpy unusable      69 no way to build a venv / install
#   75 busy: another bootstrap, or jobs are running and packages would change
#   76 the new worker didn't start; the previous one was restored and is serving
set -eu
umask 077

fail() {
  code=$1
  shift
  echo "newton-bootstrap: $*" >&2 || true
  exit "$code"
}
# Diagnostics never abort: if agentd gave up and closed our stderr, a failing
# echo must not skip the steps after it (e.g. restoring the previous worker).
warn() { echo "newton-bootstrap: warning: $*" >&2 || true; }

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
FP_HOME=$(dirname "$SCRIPT_DIR")
case "$FP_HOME" in */.newton) ;; *) fail 64 "unexpected install location: $FP_HOME" ;; esac
VENV="$FP_HOME/venv"
SOCKET="$FP_HOME/worker.sock"
PY=python3
USE_VENV=1
INSTALL_DEPS=1
STAGE=
GPU_ONLY=

while [ $# -gt 0 ]; do
  case "$1" in
    --python) [ $# -ge 2 ] || fail 64 "--python needs a value"; PY="$2"; shift 2 ;;
    --stage) [ $# -ge 2 ] || fail 64 "--stage needs a value"; STAGE="$2"; shift 2 ;;
    --no-venv) USE_VENV=0; shift ;;
    --no-deps) INSTALL_DEPS=0; shift ;;
    --gpu-only)
      [ $# -ge 2 ] || fail 64 "--gpu-only needs a value"
      case "$2" in auto|cuda) GPU_ONLY="$2" ;; *) fail 64 "--gpu-only must be auto or cuda" ;; esac
      shift 2 ;;
    *) fail 64 "unknown argument: $1" ;;
  esac
done
# sun_path is 104 bytes on macOS, 108 on Linux.
[ "${#SOCKET}" -le 100 ] || fail 64 "home directory path too long for a Unix socket: $SOCKET"
if [ -n "$STAGE" ]; then
  case "$STAGE" in src.stage-*[!0-9a-f]*|src.stage-) fail 64 "invalid stage: $STAGE" ;; src.stage-*) ;;
    *) fail 64 "invalid stage: $STAGE" ;; esac
  [ -d "$FP_HOME/$STAGE/newton_worker" ] || fail 64 "staged source missing: $FP_HOME/$STAGE"
  CODE="$FP_HOME/$STAGE"
else
  CODE="$FP_HOME/src"
fi

# A relative --python is relative to where sshd started us ($HOME), not to /.
case "$PY" in /*) ;; */*) PY="$PWD/$PY" ;; esac

# The worker runs with exactly this environment; don't let the login shell's
# Python settings or the home directory's files leak into the checks below.
cd /
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PIP_TARGET PIP_PREFIX PIP_USER
export PYTHONSAFEPATH=1
chmod 700 "$FP_HOME"
# One token file per Newton install in tokens/ (a legacy single token is accepted).
if ! ls "$FP_HOME"/tokens/* >/dev/null 2>&1 && [ ! -f "$FP_HOME/token" ]; then
  fail 65 "no worker token in $FP_HOME/tokens"
fi
chmod -R go-rwx "$FP_HOME/tokens" "$FP_HOME/token" 2>/dev/null || true

# One bootstrap at a time per host.
trap '' PIPE
LOCK_WAIT="${NEWTON_BOOTSTRAP_LOCK_WAIT:-300}"
case "$LOCK_WAIT" in *[!0-9]*|'') LOCK_WAIT=300 ;; esac
LOCK=
if command -v flock >/dev/null 2>&1 && flock --help 2>&1 | grep -q -- '-w'; then
  # Kernel lock (Linux): released automatically however this script dies.
  exec 9>"$FP_HOME/bootstrap.flock"
  flock -w "$LOCK_WAIT" 9 || fail 75 "another bootstrap is still running"
else
  # mkdir lock (macOS): records its owner's pid; a dead owner's lock (or one older
  # than an hour) is taken over by an atomic rename, so only one waiter wins.
  LOCK="$FP_HOME/bootstrap.lock"
  waited=0
  while ! mkdir "$LOCK" 2>/dev/null; do
    owner=$(cat "$LOCK/pid" 2>/dev/null || true)
    if { [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; } \
      || [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
      # Atomic takeover: rename it away, then make sure it was the dead owner's
      # (a peer may have just re-acquired it); if not, put it back.
      if mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
        if [ "$(cat "$LOCK.stale.$$/pid" 2>/dev/null || true)" = "$owner" ]; then
          rm -rf "$LOCK.stale.$$"
        elif ! mv "$LOCK.stale.$$" "$LOCK" 2>/dev/null; then
          rm -rf "$LOCK.stale.$$"
        fi
      fi
      continue
    fi
    [ "$waited" -lt "$LOCK_WAIT" ] || fail 75 "another bootstrap is still running (pid ${owner:-unknown})"
    sleep 1
    waited=$((waited + 1))
  done
  echo $$ >"$LOCK/pid" || fail 75 "lost the bootstrap lock to a concurrent bootstrap"
fi
cleanup() {
  rc=$?
  rm -rf "$VENV.new" "$FP_HOME/bootstrap.json.tmp"
  # A failed bootstrap leaves no staging dir behind (the live src is untouched).
  if [ "$rc" != 0 ] && [ -n "$STAGE" ]; then rm -rf "${FP_HOME:?}/$STAGE"; fi
  if [ -n "$LOCK" ] && [ "$(cat "$LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$LOCK"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# Staging dirs left by uploads whose bootstrap never ran.
for old in "$FP_HOME"/src.stage-*; do
  if [ -d "$old" ] && [ "$old" != "$CODE" ] && [ -n "$(find "$old" -maxdepth 0 -mmin +60)" ]; then
    rm -rf "$old"
  fi
done

command -v "$PY" >/dev/null 2>&1 || fail 66 "python not found: $PY"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 \
  || fail 66 "python >= 3.9 required, $PY is older"
py_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null; }
PY_VERSION=$(py_version "$PY")

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  # Non-interactive SSH sessions usually miss ~/.local/bin on PATH.
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}
UV=$(find_uv || true)

active_jobs() {
  # Fail closed: if we can't tell, assume jobs are running.
  PYTHONPATH="$CODE" "$PY" -m newton_worker --root "$FP_HOME" active-jobs 2>/dev/null \
    || echo unknown
}

venv_ok() {
  [ -x "$1/bin/python" ] || return 1
  [ "$(py_version "$1/bin/python")" = "$PY_VERSION" ] || return 1
  # Built from another interpreter, or the distro Python was upgraded underneath it.
  cfg=$(sed -n 's/^version[_a-z]* *= *\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$1/pyvenv.cfg" \
    2>/dev/null | head -n 1)
  if [ -n "$cfg" ] && [ "$cfg" != "$PY_VERSION" ]; then return 1; fi
  # Earlier bootstraps used --system-site-packages, which let apt's numpy 1.x leak in.
  if grep -qi '^include-system-site-packages *= *true' "$1/pyvenv.cfg" 2>/dev/null; then
    return 1
  fi
  return 0
}

create_venv() {
  rm -rf "$1"
  if "$PY" -m venv "$1" >/dev/null 2>&1; then return 0; fi
  rm -rf "$1"
  # Debian/Ubuntu without python3-venv lack ensurepip; a pip-less venv still works
  # and gets its packages from an installer outside it.
  if "$PY" -m venv --without-pip "$1" >/dev/null 2>&1; then return 0; fi
  rm -rf "$1"
  if [ -n "$UV" ] && UV_PYTHON_DOWNLOADS=never "$UV" venv --quiet --python "$PY" "$1" >&2; then
    return 0
  fi
  rm -rf "$1"
  return 1
}

VENV_STATE=none
if [ "$USE_VENV" = 1 ]; then
  if venv_ok "$VENV"; then
    VENV_STATE=reused
    VPY="$VENV/bin/python"
  elif [ -n "$GPU_ONLY" ]; then
    fail 64 "no working venv in $VENV yet: bootstrap the worker first"
  else
    VENV_STATE=created
    if [ -d "$VENV" ] && [ "$(active_jobs)" != 0 ]; then
      fail 75 "jobs are running in the current venv; rebuilding it is deferred until they finish"
    fi
    create_venv "$VENV.new" || fail 69 "cannot create a virtual environment with $PY and uv" \
      "is not installed. On Ubuntu / DGX OS run: sudo apt install python3-venv python3-pip"
    VPY="$VENV.new/bin/python"
  fi
else
  VPY="$PY"
fi

pip_supports_python_flag() {
  version=$("$PY" -m pip --version 2>/dev/null \
    | sed -n 's/^pip \([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p')
  [ -n "$version" ] || return 1
  major=${version% *}
  minor=${version#* }
  [ "$major" -gt 22 ] || { [ "$major" -eq 22 ] && [ "$minor" -ge 3 ]; }
}

# A value from pip's config files (later files win, [install] over [global]).
pip_config() {
  "$PY" - "$1" 2>/dev/null <<'PYCONF' || true
import configparser, os, sys
key, value = sys.argv[1], ""
files = ["/etc/xdg/pip/pip.conf", "/etc/pip.conf", os.path.expanduser("~/.pip/pip.conf"),
         os.path.expanduser("~/.config/pip/pip.conf"), os.environ.get("PIP_CONFIG_FILE", "")]
for path in files:
    if not path or not os.path.isfile(path):
        continue
    config = configparser.RawConfigParser()
    try:
        config.read(path)
    except configparser.Error:
        continue
    for section in ("global", "install"):
        if config.has_option(section, key):
            value = " ".join(config.get(section, key).split())
print(value)
PYCONF
}

INSTALLER=skipped
install_into_venv() {
  if "$VPY" -m pip --version >/dev/null 2>&1; then
    INSTALLER=venv-pip
    "$VPY" -m pip install --quiet --disable-pip-version-check "$@" >&2
  elif pip_supports_python_flag; then
    INSTALLER=system-pip
    "$PY" -m pip --python "$VPY" install --quiet --disable-pip-version-check "$@" >&2
  elif "$PY" -m pip --version >/dev/null 2>&1; then
    # pip < 22.3 has no --python and predates PEP 668 enforcement; --upgrade makes
    # --target replace older copies instead of piling up duplicate dist-info dirs.
    INSTALLER=system-pip-target
    site=$("$VPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    "$PY" -m pip install --quiet --disable-pip-version-check --upgrade --target "$site" "$@" >&2 \
      || return 1
    # --target never uninstalls: once the install worked, keep only the newest
    # metadata dir per package, so importlib.metadata matches what imports.
    for req in "$@"; do
      # dist-info dirs use the normalized name: cupy-cuda13x[ctk]>=14 → cupy_cuda13x
      pkg=$(printf '%s' "$req" | sed 's/[][<>=!~ ].*//; s/[-.]/_/g')
      # shellcheck disable=SC2012
      ls -dt "$site/$pkg"-*.dist-info 2>/dev/null | tail -n +2 | while IFS= read -r stale; do
        rm -rf "$stale"
      done
    done
  elif [ -n "$UV" ]; then
    INSTALLER=uv
    # uv ignores pip's configuration; carry a configured pip mirror over.
    if [ -z "${UV_DEFAULT_INDEX:-}${UV_INDEX_URL:-}" ]; then
      idx="${PIP_INDEX_URL:-$(pip_config index-url)}"
      if [ -n "$idx" ]; then UV_DEFAULT_INDEX="$idx" && export UV_DEFAULT_INDEX; fi
    fi
    if [ -z "${UV_INDEX:-}${UV_EXTRA_INDEX_URL:-}" ]; then
      extra="${PIP_EXTRA_INDEX_URL:-$(pip_config extra-index-url)}"
      if [ -n "$extra" ]; then UV_INDEX="$extra" && export UV_INDEX; fi
    fi
    UV_PYTHON_DOWNLOADS=never "$UV" pip install --quiet --python "$VPY" "$@" >&2
  else
    INSTALLER=none
    return 1
  fi
}

# numpy 2.0–2.5 is what CuPy 14 supports; matplotlib only draws report plots.
NUMPY_IN_RANGE='import sys, numpy
v = tuple(int(x) for x in numpy.__version__.split(".")[:2])
sys.exit(0 if (2, 0) <= v < (2, 6) else 3)'
NUMPY_IMPORTS='import numpy'
MPL_OK='import sys, matplotlib
v = tuple(int(x) for x in matplotlib.__version__.split(".")[:2])
sys.exit(0 if v >= (3, 9) else 3)'
works() { "$VPY" -c "$1" >/dev/null 2>&1; }

# The worker's CLI; never holds the bootstrap lock (fd 9) beyond this script.
gpu_cli() { PYTHONPATH="$CODE" "$VPY" -m newton_worker --root "$FP_HOME" gpu "$@" 9>&-; }
gpu_idle() {  # packages may change only while no job runs in this venv
  [ "$(active_jobs)" != 0 ] || return 0
  if [ "$GPU_ONLY" = cuda ]; then
    fail 75 "jobs are running; install GPU support once they finish"
  fi
  gpu_cli defer
  return 1
}
uninstall_from_venv() {  # $1 = a CuPy distribution built for the wrong CUDA major
  if "$VPY" -m pip --version >/dev/null 2>&1; then
    "$VPY" -m pip uninstall -y -q "$1" >&2
  elif pip_supports_python_flag; then
    "$PY" -m pip --python "$VPY" uninstall -y -q "$1" >&2
  elif [ -n "$UV" ]; then
    UV_PYTHON_DOWNLOADS=never "$UV" pip uninstall --python "$VPY" "$1" >&2
  else
    site=$("$VPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    rm -rf "$site/cupy" "$site/cupy_backends" "$site/$(printf '%s' "$1" | sed 's/[-.]/_/g')"-*.dist-info
  fi
}
GPU_INSTALLED=0
gpu_install() {  # $@ = requirements; the numpy pin stays in the same resolve
  gpu_idle || return 1
  GPU_INSTALLED=1
  if install_into_venv "$@" "numpy>=2,<2.6"; then return 0; fi
  gpu_cli fail --error "installing $* with $INSTALLER failed; check the host's internet access or pip index"
  return 1
}
gpu_support() {
  can_install=
  if [ "$USE_VENV" = 1 ] && [ "$INSTALL_DEPS" = 1 ]; then can_install=--can-install; fi
  plan=$(gpu_cli plan --mode "$GPU_ONLY" $can_install) || { warn "GPU support check failed"; return 0; }
  set -f
  # shellcheck disable=SC2086 # requirements are single words; globbing is off
  set -- $plan
  set +f
  action=${1:-skip}
  [ $# -eq 0 ] || shift
  case "$action" in
    ready) return 0 ;;
    skip)
      if [ "$GPU_ONLY" = cuda ]; then warn "GPU support: $*"; fi
      return 0 ;;
    install) gpu_install "$@" || return 0 ;;
    replace)
      gpu_idle || return 0
      old=$1
      shift
      uninstall_from_venv "$old" || warn "could not remove $old"
      gpu_install "$@" || return 0 ;;
    verify) ;;
    *) warn "GPU support: unexpected plan '$plan'"; return 0 ;;
  esac
  rc=0
  result=$(gpu_cli smoke --mode "$GPU_ONLY") || rc=$?
  if [ "$rc" = 3 ] && [ -n "$can_install" ]; then
    # CuPy is there but can't find usable CUDA libraries: get them from PyPI.
    fallback=$(printf '%s\n' "$result" | sed -n 's/^FALLBACK //p')
    set -f
    # shellcheck disable=SC2086
    set -- $fallback
    set +f
    if gpu_install "$@"; then
      result=$(gpu_cli smoke --mode "$GPU_ONLY" --fallback) || true
    fi
  fi
  case "$result" in *'"ok": true'*) ;; *) warn "GPU support is not usable; see $FP_HOME/gpu.json" ;; esac
  return 0
}

if [ -n "$GPU_ONLY" ]; then
  gpu_support
  # The worker serves from this venv right now: whatever pip did for CuPy (a
  # --target install rewrites numpy too), numpy must still work afterwards.
  if [ "$GPU_INSTALLED" = 1 ] && [ "$USE_VENV" = 1 ] && ! works "$NUMPY_IN_RANGE"; then
    install_into_venv "numpy>=2,<2.6" || true
    works "$NUMPY_IN_RANGE" || fail 68 "numpy stopped working after the GPU install;" \
      "connect the host again to repair its venv"
  fi
  gpu_cli state
  exit 0
fi

if [ "$USE_VENV" = 1 ] && [ "$INSTALL_DEPS" = 1 ]; then
  JOBS_RUNNING=0
  if [ "$VENV_STATE" = reused ] && { ! works "$NUMPY_IN_RANGE" || ! works "$MPL_OK"; }; then
    if [ "$(active_jobs)" != 0 ]; then JOBS_RUNNING=1; fi
  fi
  if works "$NUMPY_IN_RANGE"; then
    INSTALLER=satisfied
  elif [ "$JOBS_RUNNING" = 1 ]; then
    fail 75 "jobs are running; the numpy change waits until they finish"
  elif ! install_into_venv "numpy>=2,<2.6"; then
    if [ "$INSTALLER" = none ]; then
      fail 69 "no pip or uv available to install packages into $VENV." \
        "On Ubuntu / DGX OS run: sudo apt install python3-venv python3-pip"
    fi
    if [ "$INSTALLER" = uv ]; then
      fail 68 "installing numpy with uv failed (see output above). Check the host's" \
        "internet access or uv index (UV_DEFAULT_INDEX)."
    fi
    fail 68 "installing numpy with $INSTALLER failed (see output above). Check the host's" \
      "internet access or pip index (PIP_INDEX_URL)."
  fi
  if ! works "$MPL_OK"; then
    if [ "$JOBS_RUNNING" = 1 ]; then
      warn "jobs are running; matplotlib install skipped this time"
    else
      # Keep the numpy pin in the same resolve: matplotlib alone would pull the
      # newest numpy (and --target would install it over ours).
      install_into_venv "matplotlib>=3.9" "numpy>=2,<2.6" \
        || warn "matplotlib install failed; reports will have no plots"
    fi
  fi
fi

# The gate: a host never reports online unless the worker's interpreter, with the
# worker's environment, imports a usable numpy.
if [ "$USE_VENV" = 1 ]; then CHECK="$NUMPY_IN_RANGE"; else CHECK="$NUMPY_IMPORTS"; fi
if gate_out=$("$VPY" -c "$CHECK" 2>&1); then
  :
else
  gate=$?
  detail=$(printf '%s\n' "$gate_out" | tail -n 3)
  if [ "$gate" = 3 ]; then
    detail="numpy $("$VPY" -c 'import numpy; print(numpy.__version__)' 2>/dev/null) is outside 2.0–2.5"
  fi
  if [ "$USE_VENV" = 0 ]; then
    fail 68 "numpy is not usable with $VPY ($detail). Newton never installs into a system" \
      "Python: enable the venv for this host, or install numpy for $VPY yourself."
  elif [ "$INSTALL_DEPS" = 0 ]; then
    fail 68 "numpy is not usable in $VENV ($detail) and dependency install is disabled for this host."
  fi
  fail 68 "numpy was installed with $INSTALLER but is not usable: $detail"
fi
works "$MPL_OK" || warn "matplotlib unavailable; reports will have no plots"


printf '{"venv": "%s", "installer": "%s", "python": "%s"}\n' \
  "$VENV_STATE" "$INSTALLER" "$PY_VERSION" >"$FP_HOME/bootstrap.json.tmp"

start_worker() {  # $1 = interpreter; serves $FP_HOME/src. The worker daemonizes itself.
  PYTHONPATH="$FP_HOME/src" "$1" -m newton_worker --root "$FP_HOME" serve \
    --socket "$SOCKET" --detach >>"$FP_HOME/worker.log" 2>&1 </dev/null 9>&-
}
wait_for_worker() {  # up to 10 s for worker.json (written once the socket is bound)
  i=0
  while [ $i -lt 100 ] && [ ! -f "$FP_HOME/worker.json" ]; do
    i=$((i + 1))
    sleep 0.1
  done
  [ -f "$FP_HOME/worker.json" ]
}

# Point of no return: replace the running worker. If the new one doesn't come up,
# the previous src, venv and worker are put back.
stopped=$(PYTHONPATH="$CODE" "$VPY" -m newton_worker --root "$FP_HOME" stop) \
  || fail 67 "the running worker did not stop; see $FP_HOME/worker.log"
case "$stopped" in *'"was_running": true'*) HAD_WORKER=1 ;; *) HAD_WORKER=0 ;; esac
rm -f "$FP_HOME/worker.json"
rm -rf "$VENV.old" "$FP_HOME/src.old"
if [ "$VENV_STATE" = created ]; then
  if [ -d "$VENV" ]; then mv "$VENV" "$VENV.old"; fi
  mv "$VENV.new" "$VENV"
  VPY="$VENV/bin/python"
fi
if [ -n "$STAGE" ]; then
  if [ -d "$FP_HOME/src" ]; then mv "$FP_HOME/src" "$FP_HOME/src.old"; fi
  mv "$CODE" "$FP_HOME/src"
fi

start_worker "$VPY"
if wait_for_worker; then
  rm -rf "$VENV.old" "$FP_HOME/src.old"
  mv "$FP_HOME/bootstrap.json.tmp" "$FP_HOME/bootstrap.json" || true
  PYTHONPATH="$FP_HOME/src" "$VPY" -m newton_worker --root "$FP_HOME" status
  exit 0
fi

echo "newton-bootstrap: worker failed to start; log tail:" >&2 || true
tail -n 20 "$FP_HOME/worker.log" >&2 || true
# Maybe merely slow: never leave it running on files we are about to swap out.
PYTHONPATH="$FP_HOME/src" "$VPY" -m newton_worker --root "$FP_HOME" stop >/dev/null 2>&1 || true
pkill -f "newton_worker --root $FP_HOME serve" 2>/dev/null || true
if [ -d "$FP_HOME/src.old" ]; then
  rm -rf "$FP_HOME/src"
  mv "$FP_HOME/src.old" "$FP_HOME/src"
fi
if [ -d "$VENV.old" ]; then
  rm -rf "$VENV"
  mv "$VENV.old" "$VENV"
fi
if [ "$HAD_WORKER" = 1 ]; then
  if [ -x "$VENV/bin/python" ]; then OLD_PY="$VENV/bin/python"; else OLD_PY="$PY"; fi
  start_worker "$OLD_PY"
  if wait_for_worker; then
    echo "newton-bootstrap: restored and restarted the previous worker" >&2 || true
    exit 76  # transient: the host is serving again on its previous version
  fi
fi
exit 67
