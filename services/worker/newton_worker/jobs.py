"""File-backed job store.

Layout under <root>/jobs/<job_id>/:
    manifest.json   validated JobManifest
    status.json     written atomically by the server (create/start) and the wrapper
    workdir/        extracted job bundle; the job's cwd
    stdout.log, stderr.log
    cancel          flag file; the wrapper terminates the job when it appears
    wrapper.log     wrapper diagnostics
"""

from __future__ import annotations

import builtins
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from . import PROTOCOL_VERSION
from .fsutil import (
    UnsafePathError,
    check_relative,
    pack_paths_tar_gz,
    read_json,
    safe_extract_tar_gz,
    write_json_atomic,
)
from .gpu import cuda_refusal
from .gpu import job_env as gpu_job_env

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "timed_out", "cancelled", "lost"})
BACKENDS = frozenset({"cpu", "cuda", "metal"})
LEGACY_BACKENDS = {"gpu": "cuda"}
DEFAULT_ALLOWED_MODULES = ("benchmarks", "newton_worker.selftest")
MAX_TIMEOUT_SECONDS = 7 * 24 * 3600


class JobError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def now() -> float:
    return time.time()


def validate_manifest(
    m: Any, allowed_modules: tuple[str, ...], root: Optional[Path] = None
) -> dict[str, Any]:
    if not isinstance(m, dict):
        raise JobError(400, "manifest must be an object")
    job_id = m.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise JobError(400, "invalid job_id")
    backend = LEGACY_BACKENDS.get(m.get("backend", "cpu"), m.get("backend", "cpu"))
    if backend not in BACKENDS:
        raise JobError(400, f"backend must be one of {sorted(BACKENDS)}")
    unavailable = backend_unavailable(backend, root)
    if unavailable:
        raise JobError(422, f"{backend} backend not available on this worker: {unavailable}")
    command = m.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) and a and "\x00" not in a and len(a) < 4096 for a in command)
    ):
        raise JobError(400, "command must be a non-empty list of strings")
    validate_command(command, allowed_modules)
    timeout = m.get("timeout_seconds", 900)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not (1 <= timeout <= MAX_TIMEOUT_SECONDS)
    ):
        raise JobError(400, "timeout_seconds must be an integer in range")
    artifact_paths = m.get("artifact_paths", [])
    if not isinstance(artifact_paths, list) or not all(isinstance(p, str) for p in artifact_paths):
        raise JobError(400, "artifact_paths must be a list of strings")
    try:
        for p in artifact_paths:
            check_relative(p.rstrip("/"))
    except UnsafePathError as e:
        raise JobError(400, str(e)) from e
    out = dict(m)
    out.update(
        backend=backend,
        timeout_seconds=timeout,
        artifact_paths=artifact_paths,
    )
    return out


def backend_unavailable(backend: str, root: Optional[Path] = None) -> Optional[str]:
    """Cheap pre-flight (no imports): why this worker can't run `backend`, or None.
    The benchmark's own smoke kernel is the final word."""
    if backend == "cuda":
        if shutil.which("nvidia-smi") is None:
            return "no NVIDIA driver (nvidia-smi not found)"
        if not any(_installed(d) for d in ("cupy-cuda13x", "cupy-cuda12x", "cupy")):
            return "CuPy is not installed (install GPU support for this host)"
        if root is not None:
            return cuda_refusal(root)
    elif backend == "metal":
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return "not an Apple Silicon Mac"
        if not _installed("mlx"):
            return "MLX is not installed"
    return None


def _installed(dist: str) -> bool:
    try:
        metadata.version(dist)
    except metadata.PackageNotFoundError:
        return False
    return True


def validate_command(command: list[str], allowed_modules: tuple[str, ...]) -> None:
    """Only `python -m <allowed.module> [args...]` is executable. No shell, no -c."""
    if command[0] not in ("python", "python3"):
        raise JobError(400, "command must start with 'python'")
    if len(command) < 3 or command[1] != "-m":
        raise JobError(400, "command must be of the form ['python', '-m', <module>, ...]")
    module = command[2]
    if not MODULE_RE.match(module):
        raise JobError(400, "invalid module name")
    if not any(module == a or module.startswith(a + ".") for a in allowed_modules):
        raise JobError(403, f"module {module!r} is not in the worker allowlist")


def resolve_argv(command: list[str]) -> list[str]:
    return [sys.executable, *command[1:]]


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Reap if it's our own zombie child (the server spawns wrappers).
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    return True


def process_command_line(pid: int) -> str:
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        try:
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def wrapper_alive(pid: Any, job_dir: Path) -> bool:
    """True only for a live job wrapper of this job: after a reboot a recorded pid
    can belong to an unrelated process (and a zombie has no command line)."""
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    cmdline = process_command_line(pid)
    return "newton_worker.run_job" in cmdline and (
        str(job_dir) in cmdline or os.path.realpath(job_dir) in cmdline
    )


class JobStore:
    def __init__(
        self,
        root: Path,
        allowed_modules: tuple[str, ...] = DEFAULT_ALLOWED_MODULES,
    ) -> None:
        self.root = root
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_modules = allowed_modules
        self._lock = threading.Lock()

    # -- paths -------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise JobError(400, "invalid job_id")
        return self.jobs_dir / job_id

    def _existing(self, job_id: str) -> Path:
        d = self.job_dir(job_id)
        if not (d / "status.json").exists():
            raise JobError(404, f"unknown job {job_id}")
        return d

    # -- operations --------------------------------------------------------
    def create(self, manifest: Any) -> dict[str, Any]:
        m = validate_manifest(manifest, self.allowed_modules, self.root)
        with self._lock:
            d = self.job_dir(m["job_id"])
            if (d / "status.json").exists():
                existing = read_json(d / "manifest.json")
                if existing != m:
                    raise JobError(409, "job_id already exists with a different manifest")
                return self.status(m["job_id"])
            (d / "workdir").mkdir(parents=True, exist_ok=True)
            write_json_atomic(d / "manifest.json", m)
            status = {
                "job_id": m["job_id"],
                "state": "created",
                "protocol": PROTOCOL_VERSION,
                "created_at": now(),
                "bundle_files": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "wrapper_pid": None,
                "pid": None,
                "error": None,
            }
            write_json_atomic(d / "status.json", status)
            return status

    def put_bundle(self, job_id: str, data: bytes) -> dict[str, Any]:
        with self._lock:
            d = self._existing(job_id)
            status = read_json(d / "status.json")
            if status["state"] not in ("created", "bundled"):
                # Already started: re-uploading is a no-op (idempotent retries).
                return self._refresh(d, status)
            try:
                n = safe_extract_tar_gz(data, d / "workdir")
            except (UnsafePathError, OSError, EOFError, tarfile.TarError) as e:
                raise JobError(400, f"bad bundle: {e}") from e
            status.update(state="bundled", bundle_files=n)
            write_json_atomic(d / "status.json", status)
            return status

    def start(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            d = self._existing(job_id)
            status = read_json(d / "status.json")
            if status["state"] != "bundled":
                if status["state"] == "created":
                    raise JobError(409, "bundle not uploaded")
                return self._refresh(d, status)
            env = dict(os.environ)
            env.update(gpu_job_env(self.root))
            if env.get("CUPY_GPU_MEMORY_LIMIT") == "50%":  # unified memory, our default cap
                env["CUPY_GPU_MEMORY_LIMIT"] = self._cap_beside_services()
            pkg_parent = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_parent, env.get("PYTHONPATH")) if p)
            log = open(d / "wrapper.log", "ab")  # noqa: SIM115 - handed to the child
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "newton_worker.run_job", str(d)],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                log.close()
            status.update(state="starting", wrapper_pid=proc.pid, started_at=now())
            write_json_atomic(d / "status.json", status)
            return status

    def _cap_beside_services(self) -> str:
        """The CuPy pool cap for a job on a unified-memory GPU: half the memory, but
        never into what model services hold or the system's reserve (on a GB10 the
        two share one RAM, and overcommitting it freezes the host)."""
        from .services import GIB, ServiceStore, memory_now, reserve_bytes

        total = memory_now()["total"]
        held = ServiceStore(self.root).declared_memory()
        if not total or not held:
            return "50%"
        cap = min(total // 2, total - reserve_bytes(total) - held)
        if cap < 2 * GIB:
            raise JobError(
                409,
                f"model services hold {held / GIB:.0f} GB of this host's {total / GIB:.0f} GB: "
                "not enough memory left for a GPU job (stop or drain a service first)",
            )
        return str(int(cap))

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            d = self._existing(job_id)
            status = read_json(d / "status.json")
            if status["state"] in TERMINAL_STATES:
                return status
            if status["state"] in ("created", "bundled"):
                status.update(state="cancelled", finished_at=now(), error="cancelled before start")
                write_json_atomic(d / "status.json", status)
                return status
            (d / "cancel").touch()
            return self._refresh(d, status)

    def status(self, job_id: str) -> dict[str, Any]:
        d = self._existing(job_id)
        return self._refresh(d, read_json(d / "status.json"))

    def list(self) -> builtins.list[dict[str, Any]]:
        out = []
        for d in sorted(self.jobs_dir.iterdir()):
            if (d / "status.json").exists():
                out.append(self._refresh(d, read_json(d / "status.json")))
        return out

    def logs(self, job_id: str, stream: str, offset: int, limit: int) -> dict[str, Any]:
        if stream not in ("stdout", "stderr"):
            raise JobError(400, "stream must be stdout or stderr")
        d = self._existing(job_id)
        path = d / f"{stream}.log"
        size = path.stat().st_size if path.exists() else 0
        offset = max(0, min(offset, size))
        limit = max(1, min(limit, 1024 * 1024))
        data = b""
        if path.exists():
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(limit)
        next_offset = offset + len(data)
        status = self.status(job_id)
        return {
            "job_id": job_id,
            "stream": stream,
            "offset": offset,
            "next_offset": next_offset,
            "data": data.decode("utf-8", errors="replace"),
            "eof": status["state"] in TERMINAL_STATES and next_offset >= size,
        }

    def artifacts(self, job_id: str) -> bytes:
        d = self._existing(job_id)
        manifest = read_json(d / "manifest.json")
        return pack_paths_tar_gz(d / "workdir", manifest.get("artifact_paths", []))

    # -- internals ---------------------------------------------------------
    def _refresh(self, d: Path, status: dict[str, Any]) -> dict[str, Any]:
        """Detect wrappers that died without recording a terminal state."""
        if status["state"] in ("starting", "running") and not wrapper_alive(
            status.get("wrapper_pid"), d
        ):
            # Re-read: the wrapper may have finished between our read and the check.
            status = read_json(d / "status.json")
            if status["state"] in ("starting", "running"):
                status.update(
                    state="lost",
                    finished_at=now(),
                    error="job wrapper exited without recording a result",
                )
                write_json_atomic(d / "status.json", status)
        return status


def terminate_group(proc: subprocess.Popen[bytes], grace: float = 5.0) -> None:
    """SIGTERM the job's process group, then SIGKILL after a grace period."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        # Also reaches grandchildren that ignored SIGTERM.
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
