"""Detached job wrapper: `python -m newton_worker.run_job <job_dir>`.

Runs in its own session so it outlives the worker server and the SSH tunnel.
It owns the job's status.json from `starting` until a terminal state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .fsutil import read_json, write_json_atomic
from .jobs import resolve_argv, terminate_group

PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CUDA_VISIBLE_DEVICES",
    "CUDA_HOME",
    "LD_LIBRARY_PATH",
    "NVIDIA_VISIBLE_DEVICES",
    "CUDA_PATH",
    "CUPY_CACHE_DIR",  # set by the worker: Newton's private kernel cache
    "CUPY_GPU_MEMORY_LIMIT",  # set by the worker on unified-memory GPUs
)


def job_env(manifest: dict[str, Any], workdir: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    env.update(
        PYTHONPATH=os.pathsep.join(p for p in (str(workdir), os.environ.get("PYTHONPATH")) if p),
        PYTHONUNBUFFERED="1",
        MPLBACKEND="Agg",
        NEWTON_JOB_ID=manifest["job_id"],
        NEWTON_BACKEND=manifest["backend"],
    )
    return env


def run(job_dir: Path, poll_interval: float = 0.2) -> dict[str, Any]:
    manifest = read_json(job_dir / "manifest.json")
    status = read_json(job_dir / "status.json")
    if status["state"] != "starting":
        return status  # someone else already owns this job
    workdir = job_dir / "workdir"
    argv = resolve_argv(manifest["command"])
    timeout = float(manifest["timeout_seconds"])

    def update(**fields: Any) -> None:
        status.update(fields)
        write_json_atomic(job_dir / "status.json", status)

    try:
        with open(job_dir / "stdout.log", "ab") as out, open(job_dir / "stderr.log", "ab") as err:
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=job_env(manifest, workdir),
                start_new_session=True,
                close_fds=True,
            )
    except OSError as e:
        update(state="failed", finished_at=time.time(), error=f"failed to spawn: {e}")
        return status

    started = time.time()
    update(state="running", pid=proc.pid, started_at=started)
    final_state = None
    error = None
    while True:
        code = proc.poll()
        if code is not None:
            break
        if (job_dir / "cancel").exists():
            terminate_group(proc)
            final_state, error = "cancelled", "cancelled by request"
        elif time.time() - started > timeout:
            terminate_group(proc)
            final_state, error = "timed_out", f"exceeded timeout of {int(timeout)}s"
        if final_state is not None:
            code = proc.wait()
            break
        time.sleep(poll_interval)

    if final_state is None:
        final_state = "succeeded" if code == 0 else "failed"
        if code != 0:
            error = f"exit code {code}"
    update(
        state=final_state,
        exit_code=code,
        finished_at=time.time(),
        duration_seconds=time.time() - started,
        error=error,
    )
    return status


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m newton_worker.run_job <job_dir>")
    job_dir = Path(sys.argv[1])
    try:
        run(job_dir)
    except Exception as e:
        status = read_json(job_dir / "status.json")
        status.update(state="failed", finished_at=time.time(), error=f"wrapper error: {e!r}")
        write_json_atomic(job_dir / "status.json", status)
        raise


if __name__ == "__main__":
    main()
