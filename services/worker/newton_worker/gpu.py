"""GPU support (CUDA via CuPy): which package fits the driver, and whether it
really works here.

bootstrap.sh drives this in three steps, so the package install itself goes
through the same installer chain (venv pip → system pip → uv) as numpy:

    gpu plan  --mode auto|cuda   → "ready" | "skip <reason>" | "verify" | "install <req>..."
    (bootstrap.sh installs <req>... into the venv)
    gpu smoke --mode ...         → runs a CUDA smoke test in a child process with a
                                   timeout and records the outcome in gpu.json; if
                                   CuPy can't find the CUDA libraries it prints the
                                   PyPI toolkit requirements to try instead (exit 3).

gpu.json is the single source of truth for "can this host run cuda": the hardware
probe reports it, agentd derives the host's capabilities from it, and the job
pre-flight refuses cuda jobs while it records a failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from .fsutil import write_json_atomic

# Driver R580+ ships CUDA 13; CUDA 12 runs on 525+ (minor-version compatibility).
CUDA13_MIN_DRIVER = 580
CUDA12_MIN_DRIVER = 525
CUPY_DISTS = ("cupy-cuda13x", "cupy-cuda12x", "cupy")
REQUIREMENTS = {
    "cuda13": ("cupy-cuda13x>=14.0.1,<15",),
    "cuda12": ("cupy-cuda12x>=13.6,<15",),
}
# CUDA libraries from PyPI, for hosts with only the driver (no usable system
# toolkit): CuPy's `ctk` extra pulls NVIDIA's cuda-toolkit wheels (NVRTC, cuBLAS,
# ...), which CuPy then prefers over /usr/local/cuda. CUDA 13 is pinned to 13.0,
# the toolkit the R580 driver shipped with; CUDA 12 takes the newest 12.x, since
# only NVRTC >= 12.9 can compile for sm_121 (GB10).
TOOLKIT_LIBS = "[cublas,cudart,cufft,curand,cusolver,cusparse,nvrtc]"
TOOLKIT_FALLBACK = {
    "cuda13": ("cupy-cuda13x[ctk]>=14.0.1,<15", f"cuda-toolkit{TOOLKIT_LIBS}==13.0.*"),
    "cuda12": ("cupy-cuda12x[ctk]>=14,<15", f"cuda-toolkit{TOOLKIT_LIBS}==12.*"),
}
EXPECTED_DIST = {"cuda13": "cupy-cuda13x", "cuda12": "cupy-cuda12x"}
# CuPy 14 (needed for CUDA 13) has no wheels before Python 3.10; CuPy 13.6
# (cupy-cuda12x, no `ctk` extra) still supports 3.9.
MIN_PYTHON = {"cuda13": (3, 10), "cuda12": (3, 9)}
# On unified-memory GPUs (GB10) GPU allocations come out of system RAM, and
# sustained pressure can hang the whole machine instead of failing cleanly; cap
# CuPy's pool unless the host's owner set a limit themselves.
UNIFIED_MEMORY_LIMIT = "50%"
# A smoke failure that means "CUDA libraries not found / unusable", which the
# PyPI toolkit fixes (as opposed to no device, a driver problem, or a crash).
LIBRARY_ERRORS = re.compile(
    r"libnvrtc|libcublas|libcudart|nvrtc|CUDA_PATH|cannot open shared object|"
    r"CompileException|failed to load|could not find cuda|cuda toolkit|"
    r"DynamicLibNotFoundError|Failure finding|gpu-architecture|no kernel image|NO_BINARY_FOR_GPU",
    re.IGNORECASE,
)
# ...but the driver's own libraries come with the driver, never from PyPI.
DRIVER_LIBRARIES = re.compile(r"libcuda\.so|libnvidia-ml", re.IGNORECASE)
SMOKE_TIMEOUT = 300.0  # first NVRTC compile + kernel cache warm-up
RETRY_AFTER_FAILURE = 24 * 3600.0  # "auto" doesn't re-download on every bootstrap

SMOKE_CODE = r"""
import json, sys, time


def main():
    t0 = time.time()
    import cupy
    out = {"cupy": cupy.__version__}
    rt = cupy.cuda.runtime
    out["devices"] = rt.getDeviceCount()
    if out["devices"] < 1:
        raise SystemExit("no CUDA device visible to CuPy")
    props = rt.getDeviceProperties(0)
    name = props["name"]
    out["device"] = name.decode() if isinstance(name, bytes) else str(name)
    out["compute_cap"] = "%d.%d" % (props["major"], props["minor"])
    out["cuda_runtime"] = rt.runtimeGetVersion()
    out["cuda_driver"] = rt.driverGetVersion()
    # Compiled with NVRTC for this GPU's architecture: the step that fails when the
    # toolkit is too old for the GPU (e.g. sm_121 on a GB10 needs CUDA >= 12.9).
    twice = cupy.ElementwiseKernel("float64 x", "float64 y", "y = 2.0 * x + 1.0", "newton_smoke")
    n = 1 << 16
    b = twice(cupy.arange(n, dtype=cupy.float64))
    # Exact checks: the first values, and sum(2i + 1, i < n) = n^2 (exact in float64).
    head = [float(v) for v in b[:256].get().tolist()]
    ok = head == [2.0 * i + 1.0 for i in range(256)] and float(b.sum()) == float(n * n)
    # Every op the benchmark schemes use, in float64 and float32, then a sync.
    a = cupy.arange(n, dtype=cupy.float64)
    for dtype in (cupy.float64, cupy.float32):
        c = a.astype(dtype)
        float(cupy.where(cupy.roll(c, 1) > c, cupy.abs(c), cupy.maximum(c, 1.0)).sum())
    cupy.cuda.Device().synchronize()
    free, total = rt.memGetInfo()
    out["memory_free_bytes"], out["memory_total_bytes"] = int(free), int(total)
    cupy.get_default_memory_pool().free_all_blocks()
    out["results_match"] = ok
    out["seconds"] = round(time.time() - t0, 3)
    print("NEWTON-GPU-SMOKE " + json.dumps(out))


try:
    main()
except BaseException as e:
    # One parseable line with the whole message: CuPy's import errors and
    # cuda-pathfinder's "library not found" span many lines.
    error = {"type": type(e).__name__, "message": str(e)[:4000]}
    print("NEWTON-GPU-ERROR " + json.dumps(error), flush=True)
    sys.exit(1)
"""


def state_path(root: Path) -> Path:
    return root / "gpu.json"


def read_state(root: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(state_path(root).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cache_dir(root: Path) -> Path:
    """CuPy's kernel cache: private to Newton, kept across jobs (compiles once)."""
    return root / "cache" / "cupy"


def installed_cupy() -> tuple[Optional[str], Optional[str]]:
    """(distribution, version) of the CuPy this interpreter would import."""
    for dist in CUPY_DISTS:
        try:
            return dist, metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return None, None


def run_bounded(
    argv: list[str], timeout: float, **kwargs: Any
) -> tuple[Optional[int], str, str, bool]:
    """(returncode, stdout, stderr, timed_out) of a child that can never wedge us.

    subprocess.run waits forever for a killed child to exit, and a process stuck
    in the NVIDIA driver (uninterruptible) never does: then we give up on it after
    a few seconds (returncode None) instead of hanging the bootstrap."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, _text(out), _text(err), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return None, "", "", True
        return proc.returncode, _text(out), _text(err), True


def _text(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def driver_version() -> tuple[Optional[str], Optional[str]]:
    """(driver version, error). (None, None) means there is no NVIDIA driver."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None, None
    try:
        rc, out, err, timed_out = run_bounded(
            [exe, "--query-gpu=driver_version", "--format=csv,noheader"], timeout=15
        )
    except OSError as e:
        return None, f"nvidia-smi failed: {e}"
    if timed_out:
        return None, "nvidia-smi did not answer within 15 s"
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if rc != 0 or not lines:
        tail = (err or out).strip().splitlines()[-1:] or ["no output"]
        return None, f"nvidia-smi failed: {tail[0]}"
    return lines[0], None


def toolkit_for(driver: str) -> Optional[str]:
    try:
        major = int(driver.split(".")[0])
    except ValueError:
        return None
    if major >= CUDA13_MIN_DRIVER:
        return "cuda13"
    if major >= CUDA12_MIN_DRIVER:
        return "cuda12"
    return None


def _python_too_old(toolkit: str) -> Optional[str]:
    need = MIN_PYTHON[toolkit]
    if sys.version_info >= need:
        return None
    return (
        "CuPy for {} needs Python >= {}.{}; the worker runs Python {}.{} (pick a newer "
        "python for this host)".format(toolkit.upper(), *need, *sys.version_info[:2])
    )


def plan(root: Path, mode: str, can_install: bool) -> list[str]:
    """What bootstrap.sh should do: ["ready"], ["skip", reason...], ["verify"],
    ["install", req...] or ["replace", dist, req...] (uninstall dist first).

    Failures that need no install are recorded right here, as preconditions
    (attempted=False): they are re-evaluated on every bootstrap, so fixing them
    (newer Python, installs enabled) takes effect at once. Only real attempts
    (an install or a smoke test) are throttled to one retry a day in "auto"."""
    state = read_state(root) or {}
    driver, error = driver_version()
    if driver is None:
        if error is None:
            if mode == "cuda":
                record(
                    root, ok=False, attempted=False, driver=None,
                    error="no NVIDIA driver on this host (nvidia-smi not found)",
                )  # fmt: skip
            return ["skip", "no NVIDIA driver"]
        if state.get("ok"):
            # Maybe a hiccup: keep the verified result. The hardware probe reports
            # the nvidia-smi error, and agentd won't place cuda work meanwhile.
            return ["skip", "driver problem"]
        record(root, ok=False, attempted=False, driver=None, error=error)
        return ["skip", "driver problem"]
    toolkit = toolkit_for(driver)
    dist, version = installed_cupy()
    if dist is not None:
        same = (
            state.get("driver") == driver
            and state.get("cupy") == version
            and state.get("dist") == dist
        )
        expected = EXPECTED_DIST.get(toolkit or "")
        wrong_build = dist in EXPECTED_DIST.values() and expected and dist != expected
        if (
            wrong_build
            and can_install
            and toolkit is not None
            and not _python_too_old(toolkit)
            and (mode == "cuda" or (same and not state.get("ok")))
        ):
            # e.g. cupy-cuda13x after a driver downgrade below R580.
            return ["replace", dist, *REQUIREMENTS[toolkit]]
        if same and state.get("ok") and mode != "cuda":
            return ["ready"]  # an explicit request always re-runs the smoke test
        if same and mode == "auto" and state.get("attempted") and not _retry_due(state):
            return ["skip", "failed before on this driver and CuPy"]
        return ["verify"]
    if toolkit is None:
        record(
            root, ok=False, attempted=False, driver=driver,
            error=f"NVIDIA driver {driver} is too old for CuPy (needs >= {CUDA12_MIN_DRIVER})",
        )  # fmt: skip
        return ["skip", "driver too old"]
    too_old = _python_too_old(toolkit)
    if too_old:
        record(root, ok=False, attempted=False, driver=driver, error=too_old)
        return ["skip", "python too old for CuPy"]
    if not can_install:
        record(
            root, ok=False, attempted=False, driver=driver,
            error="CuPy is not installed and package installs are disabled for this host "
            f"(install {REQUIREMENTS[toolkit][0]} for the worker's Python yourself)",
        )  # fmt: skip
        return ["skip", "installs disabled"]
    if (
        mode == "auto"
        and state.get("attempted")
        and state.get("driver") == driver
        and state.get("toolkit") == toolkit
        and not state.get("ok")
        and not _retry_due(state)
    ):
        return ["skip", "install failed before on this driver"]
    return ["install", *REQUIREMENTS[toolkit]]


def _retry_due(state: dict[str, Any]) -> bool:
    if state.get("deferred"):
        return True  # it never ran (jobs were running): try again
    checked = state.get("checked_at")
    return not isinstance(checked, (int, float)) or time.time() - checked > RETRY_AFTER_FAILURE


def record(root: Path, ok: bool, **fields: Any) -> dict[str, Any]:
    fields.setdefault("attempted", True)
    if "driver" not in fields:
        fields["driver"] = driver_version()[0]
    if "toolkit" not in fields and fields.get("driver"):
        fields["toolkit"] = toolkit_for(str(fields["driver"]))
    dist, version = installed_cupy()
    state = {"ok": ok, "checked_at": time.time(), "dist": dist, "cupy": version, **fields}
    write_json_atomic(state_path(root), state)
    return state


def smoke_env(root: Path) -> dict[str, str]:
    """The environment CUDA jobs get (run_job passes the same variables on)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CUDA_PATH", "CUDA_HOME",
                 "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES")
    }  # fmt: skip
    env.update(job_env(root))
    env["PYTHONSAFEPATH"] = "1"
    env["CUPY_DEBUG_LIBRARY_LOAD"] = "1"  # where each CUDA library came from, for the log
    return env


def smoke(root: Path, timeout: Optional[float] = None) -> dict[str, Any]:
    """Run the smoke test in a child interpreter (a crash, e.g. SIGBUS, or a hang
    must not take the caller down) and record the outcome."""
    if timeout is None:
        timeout = float(os.environ.get("NEWTON_GPU_SMOKE_TIMEOUT") or SMOKE_TIMEOUT)
    cache_dir(root).mkdir(parents=True, exist_ok=True)
    unified = _unified_memory()
    try:
        rc, out, err, timed_out = run_bounded(
            [sys.executable, "-c", SMOKE_CODE], timeout, env=smoke_env(root), cwd=str(root)
        )
    except OSError as e:
        return record(root, ok=False, error=f"could not run the CUDA smoke test: {e}")
    log = err.strip().splitlines()[-30:]
    common: dict[str, Any] = {"log": log, "unified_memory": unified}
    if timed_out:
        stuck = " and could not be stopped (stuck in the driver?)" if rc is None else ""
        return record(
            root, ok=False, error=f"the CUDA smoke test hung for {timeout:.0f} s{stuck}", **common
        )
    crash = _signal_name(rc or 0)
    if crash:
        return record(root, ok=False, error=f"the CUDA smoke test crashed ({crash})", **common)
    found = _tagged(out, "NEWTON-GPU-SMOKE ")
    if rc != 0 or found is None:
        failure = _tagged(out, "NEWTON-GPU-ERROR ") or {}
        message = str(failure.get("message") or "")
        kind = str(failure.get("type") or "")
        if failure:
            summary = f"{kind}: {_summary(message)}" if message else kind
        else:
            lines = (err or out).strip().splitlines()
            summary = lines[-1] if lines else f"exit code {rc}"
        return record(
            root, ok=False, error=f"CUDA smoke test failed: {summary[:500]}",
            detail=f"{kind}: {message}" if failure else None, **common,
        )  # fmt: skip
    if not found.get("results_match"):
        return record(root, ok=False, smoke=found, error="the GPU computed wrong results", **common)
    return record(root, ok=True, smoke=found, error=None, **common)


def _tagged(out: str, tag: str) -> Optional[dict[str, Any]]:
    lines = [ln for ln in out.splitlines() if ln.startswith(tag)]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1][len(tag) :])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _summary(message: str) -> str:
    """The informative line of a long error: the one naming a library or an
    error, not CuPy's "=====" banner or pathfinder's directory listings."""
    lines = [ln.strip() for ln in message.splitlines() if ln.strip().strip("=")]
    for line in lines:
        if DRIVER_LIBRARIES.search(line) or LIBRARY_ERRORS.search(line):
            return line
    for line in lines:
        if re.search(r"error|exception|failed|failure", line, re.IGNORECASE):
            return line
    return lines[0] if lines else ""


def _signal_name(returncode: int) -> Optional[str]:
    """Killed by a signal: negative from subprocess, or 128+n through a shell
    (a SIGBUS shows up as 135)."""
    if returncode < 0:
        number = -returncode
    elif returncode > 128 and returncode - 128 < 65:
        number = returncode - 128
    else:
        return None
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _unified_memory() -> bool:
    from .hardware import probe_gpus  # hardware imports this module

    gpus, _ = probe_gpus()
    return any(g.get("unified_memory") for g in gpus)


def job_env(root: Path) -> dict[str, str]:
    """Extra environment for a job on this worker (CUDA cache, memory cap)."""
    env = {"CUPY_CACHE_DIR": str(cache_dir(root))}
    state = read_state(root)
    if state and state.get("unified_memory") and "CUPY_GPU_MEMORY_LIMIT" not in os.environ:
        env["CUPY_GPU_MEMORY_LIMIT"] = UNIFIED_MEMORY_LIMIT
    return env


def toolkit_fallback(state: dict[str, Any]) -> Optional[tuple[str, ...]]:
    """PyPI CUDA libraries to try when the failure says CUDA libraries are missing."""
    text = "\n".join(
        [str(state.get("error") or ""), str(state.get("detail") or "")]
        + list(state.get("log") or [])
    )
    if state.get("ok") or not LIBRARY_ERRORS.search(text) or DRIVER_LIBRARIES.search(text):
        return None
    toolkit = state.get("toolkit")
    if toolkit not in TOOLKIT_FALLBACK or state.get("fallback_tried"):
        return None
    if state.get("dist") != EXPECTED_DIST[str(toolkit)]:
        return None  # another CuPy build: don't install a second one beside it
    if _python_too_old("cuda13"):
        return None  # the `ctk` extra exists from CuPy 14 on
    return TOOLKIT_FALLBACK[str(toolkit)]


def cuda_refusal(root: Path) -> Optional[str]:
    """Job pre-flight: the recorded failure for the installed CuPy and driver."""
    state = read_state(root)
    if not state or state.get("ok"):
        return None
    _, version = installed_cupy()
    if state.get("cupy") != version or state.get("driver") != driver_version()[0]:
        return None  # recorded for another CuPy or driver: the benchmark's check decides
    return f"GPU support failed its check on this host: {state.get('error')}"


def main(argv: list[str], root: Path) -> int:
    """`python -m newton_worker gpu {plan,smoke,defer,fail,state} ...` (bootstrap.sh)."""
    cmd, args = argv[0], argv[1:]
    mode = args[args.index("--mode") + 1] if "--mode" in args else "auto"
    prev = read_state(root) or {}
    if cmd == "plan":
        print(" ".join(plan(root, mode, can_install="--can-install" in args)))
        return 0
    if cmd == "smoke":
        state = smoke(root)
        same = all(prev.get(k) == state.get(k) for k in ("driver", "dist", "cupy"))
        if "--fallback" in args or (not state["ok"] and same and prev.get("fallback_tried")):
            # The PyPI toolkit was tried for exactly this setup: never again.
            state = record(root, **{**_fields(state), "fallback_tried": True})
        print(json.dumps(state))
        fallback = toolkit_fallback(state)
        if fallback and "--fallback" not in args:
            print("FALLBACK " + " ".join(fallback))
            return 3
        return 0 if state["ok"] else 1
    if cmd == "defer":
        record(
            root, ok=False, deferred=True, error="jobs were running; GPU support waits",
            cause=prev.get("error"), log=prev.get("log"), unified_memory=prev.get("unified_memory"),
        )  # fmt: skip
        return 0
    if cmd == "fail":
        message = args[args.index("--error") + 1] if "--error" in args else "install failed"
        record(
            root, ok=False, error=message, cause=prev.get("error"), log=prev.get("log"),
            unified_memory=prev.get("unified_memory"),
        )  # fmt: skip
        return 0
    if cmd == "state":
        print(json.dumps(prev or None))
        return 0
    print(f"unknown gpu command: {cmd}", file=sys.stderr)
    return 64


def _fields(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if k not in ("checked_at", "dist", "cupy")}
