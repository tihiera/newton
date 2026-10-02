"""Local runner: the worker runs as a child process on the Mac."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .base import RunnerError
from .worker_client import WorkerClient, WorkerClientRunner


def _is_our_worker(pid: int, root: Path) -> bool:
    """Only signal a pid that is still a worker for this root: after a crash and
    reboot the recorded pid may belong to an unrelated process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # not ours to signal
    try:
        cmdline = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "newton_worker" in cmdline and " serve" in cmdline and str(root) in cmdline


class LocalRunner(WorkerClientRunner):
    def __init__(self, host_id: str, root: Path, token: str, worker_source: Path) -> None:
        self.host_id = host_id
        self.root = root
        self.token = token
        self.worker_source = worker_source
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None and self.client is not None:
                return
            await self._start()

    def _kill_orphan(self) -> None:
        """A previous agentd that died hard may have left its worker running."""
        info_path = self.root / "worker.json"
        if not info_path.exists():
            return
        try:
            pid = int(json.loads(info_path.read_text())["pid"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return
        if pid != os.getpid() and _is_our_worker(pid, self.root):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def _start(self) -> None:
        await self.close()
        self._kill_orphan()
        self.root.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["NEWTON_WORKER_TOKEN"] = self.token
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(self.worker_source), env.get("PYTHONPATH")) if p
        )
        log = open(self.root / "worker.log", "ab")  # noqa: SIM115 - handed to the child
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "newton_worker",
            "--root",
            str(self.root),
            "serve",
            "--port",
            "0",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
            env=env,
        )
        log.close()
        assert self.proc.stdout is not None
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=15)
            port = int(json.loads(line)["port"])
        except (TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
            await self._stop_proc()
            tail = (self.root / "worker.log").read_text(errors="replace")[-500:]
            raise RunnerError(f"local worker failed to start: {e!r} {tail}") from e
        self.client = WorkerClient(f"http://127.0.0.1:{port}", self.token)

    async def _stop_proc(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.proc = None

    async def close(self) -> None:
        await super().close()
        await self._stop_proc()
