"""Detached service supervisor: `python -m newton_worker.run_service <service_dir>`.

Runs in its own session so it outlives the worker and the SSH tunnel, like
run_job. It owns status.json from `starting` until a terminal state:

  starting  engine process spawned, its HTTP server not up yet
  loading   server up; preparing the model (ollama: pull, check the pinned
            digest, load it resident)
  ready     answering; health checked every few seconds (`healthy`)
  draining  the router sends nothing new; stops at drain_until
  stopping  -> stopped    (asked to stop, or drained)
  failed    the engine exited, timed out starting, or the model didn't match
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .fsutil import read_json, write_json_atomic
from .procs import identity, stop_group
from .services import GIB, MLX_COMPLETE, gated, mac_gate

# The engine is on 127.0.0.1: never through a proxy from the environment (urllib
# would send even loopback requests to HTTP_PROXY, and the service never got ready).
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
POLL = 0.5
HEALTH_EVERY = float(os.environ.get("NEWTON_SERVICE_HEALTH_EVERY") or 5.0)
UNHEALTHY_AFTER = 3  # consecutive failed health checks
STOP_GRACE = float(os.environ.get("NEWTON_SERVICE_STOP_GRACE") or 20.0)  # then SIGKILL


class Stop(Exception):
    """Asked to stop (SIGTERM or the stop file), or a Mac model's gate closed."""

    def __init__(self, reason: Optional[str] = None) -> None:
        super().__init__(reason or "stop")
        self.reason = reason


# Set by the SIGTERM handler, which main() installs before anything else: a stop
# that arrives while the interpreter is still starting must not kill the supervisor
# with the default action (that would read as "lost").
STOP_SIGNALLED = False
INTERRUPTIBLE = False  # only while waiting on a long call (a model download)


def _unblock_sigterm() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


def _on_sigterm(*_: Any) -> None:
    global STOP_SIGNALLED
    STOP_SIGNALLED = True
    if INTERRUPTIBLE:
        raise Stop


class Supervisor:
    def __init__(self, service_dir: Path) -> None:
        self.dir = service_dir
        self.spec: dict[str, Any] = read_json(service_dir / "spec.json")
        self.status: dict[str, Any] = read_json(service_dir / "status.json")
        self.port = int(self.status["port"])
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._next_gate = 0.0
        self._gate_failures = 0
        self.engine_identity: Optional[dict[str, Any]] = None

    # -- bookkeeping -------------------------------------------------------------
    def update(self, **fields: Any) -> None:
        self.status.update(fields)
        write_json_atomic(self.dir / "status.json", self.status)

    def check_stop(self) -> None:
        if STOP_SIGNALLED or (self.dir / "stop").exists():
            raise Stop

    def interruptible(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        """A long blocking call that a stop may interrupt (SIGTERM raises Stop)."""
        global INTERRUPTIBLE
        self.check_stop()
        INTERRUPTIBLE = True
        try:
            return call(*args, **kwargs)
        finally:
            INTERRUPTIBLE = False

    def drain_deadline(self) -> Optional[float]:
        try:
            value = read_json(self.dir / "control.json").get("drain_until")
        except (OSError, ValueError):
            return None
        return float(value) if isinstance(value, (int, float)) else None

    def engine_error(self, prefix: str) -> str:
        code = self.proc.poll() if self.proc else None
        tail = ""
        try:
            lines = (self.dir / "engine.log").read_text(errors="replace").strip().splitlines()
            tail = " | ".join(lines[-5:])
        except OSError:
            pass
        return (
            f"{prefix} (exit code {code}): {tail}"[:2000]
            if tail
            else f"{prefix} (exit code {code})"
        )

    # -- HTTP to the engine (127.0.0.1 only) --------------------------------------
    def http(self, method: str, path: str, body: Any = None, timeout: float = 5.0) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 URL
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )  # fmt: skip
        with DIRECT.open(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def up(self) -> bool:
        path = "/api/version" if self.spec["engine"] == "ollama" else "/health"
        try:
            self.http("GET", path, timeout=3)
        except (OSError, urllib.error.URLError, ValueError):
            return False
        return True

    def named(self, entries: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        model = self.spec["model"]
        return next((m for m in entries if model in (m.get("name"), m.get("model"))), None)

    def healthy(self) -> bool:
        try:
            if self.spec["engine"] == "ollama":
                loaded = self.http("GET", "/api/ps", timeout=5).get("models") or []
                return self.named(loaded) is not None
            self.http("GET", "/health", timeout=5)
        except (OSError, urllib.error.URLError, ValueError, AttributeError):
            return False
        return True

    # -- lifecycle -----------------------------------------------------------------
    def spawn(self) -> None:
        command = read_json(self.dir / "command.json")
        env = {**os.environ, **command["env"]}
        # No stop may land between fork and recording the engine: it would leave an
        # engine running that nothing knows about.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            with open(self.dir / "engine.log", "ab") as log:
                self.proc = subprocess.Popen(
                    command["argv"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    env=env, cwd=str(self.dir), start_new_session=True, close_fds=True,
                    # A blocked signal stays blocked across exec: the engine must get
                    # SIGTERM normally. (The supervisor is single-threaded: safe.)
                    preexec_fn=_unblock_sigterm,
                )  # fmt: skip
            self.engine_identity = identity(self.proc.pid)
            self.update(engine_pid=self.proc.pid, engine_identity=self.engine_identity,
                        supervisor_pid=os.getpid())  # fmt: skip
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        self.check_stop()

    def wait_up(self, deadline: float) -> None:
        while True:
            self.check_stop()
            # Checked around up(): whatever answers on the port must be our engine
            # (another process could have taken the port before it bound it).
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(self.engine_error("the engine exited while starting"))
            if self.up() and self.proc and self.proc.poll() is None:
                return
            self.check_gate()
            if time.time() > deadline:
                raise RuntimeError(self.engine_error("the engine did not start in time"))
            time.sleep(POLL)

    def fetch(self, deadline: float) -> None:
        """MLX: download the pinned snapshot before the engine starts (fixed argv from
        command.json). Its size is checked against what was declared before (from the
        hub's file listing) and after (what landed); a stop ends the download's process
        group, which is recorded, so a lost supervisor's download can be ended too."""
        command = read_json(self.dir / "command.json")
        argv = command.get("fetch")
        if not argv:
            return
        self.update(state="loading")
        engine_argv = command["argv"]
        snapshot = Path(engine_argv[engine_argv.index("--model") + 1])
        if (snapshot / MLX_COMPLETE).is_file():
            return  # downloaded and checked before
        limit = float(self.spec["memory_gb"]) * GIB
        listed = self.listed_weights()
        if listed is not None and listed > limit:
            raise RuntimeError(self.too_big(listed))
        env = {**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1"}
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            with open(self.dir / "engine.log", "ab") as log:
                fetch = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         cwd=str(self.dir), start_new_session=True,
                                         close_fds=True, env=env,
                                         preexec_fn=_unblock_sigterm)  # fmt: skip
            fetch_identity = identity(fetch.pid)
            self.update(fetch_pid=fetch.pid, fetch_identity=fetch_identity,
                        supervisor_pid=os.getpid())  # fmt: skip
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        try:
            while fetch.poll() is None:
                self.check_stop()
                self.check_gate()
                if time.time() > deadline:
                    raise RuntimeError("the model download did not finish in time")
                time.sleep(POLL)
        finally:
            if fetch.poll() is None:
                stop_group(fetch.pid, fetch_identity, grace=5)
            self.update(fetch_pid=None, fetch_identity=None)
        if fetch.returncode != 0:
            raise RuntimeError(self.engine_error(f"the model download failed ({fetch.returncode})"))
        if not (snapshot / "config.json").is_file():
            raise RuntimeError(f"the pinned revision {self.spec['revision']} was not downloaded")
        landed = sum(f.stat().st_size for f in snapshot.glob("*.safetensors"))
        if landed > limit:
            raise RuntimeError(self.too_big(landed))
        (snapshot / MLX_COMPLETE).write_text(json.dumps({"bytes": landed, "at": time.time()}))

    def listed_weights(self) -> Optional[int]:
        """The weights' size at the pinned revision, from the hub's file listing (None:
        couldn't ask; the size is checked again after the download)."""
        url = (f"https://huggingface.co/api/models/{self.spec['model']}/tree/"
               f"{self.spec['revision']}")  # fmt: skip
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - fixed https
                files = json.loads(resp.read())
        except (OSError, urllib.error.URLError, ValueError):
            return None
        if not isinstance(files, list):
            return None
        weights = [
            f
            for f in files
            if isinstance(f, dict) and str(f.get("path", "")).endswith(".safetensors")
        ]
        return sum(int(f.get("size") or 0) for f in weights)

    def too_big(self, size: float) -> str:
        return (f"{self.spec['model']}'s weights are {size / GIB:.1f} GB, more than the "
                f"{self.spec['memory_gb']} GB declared: not a small model")  # fmt: skip

    def gate(self) -> Optional[str]:
        """Why a Mac model must stop now (on battery, memory pressure), or None."""
        if not gated(self.spec):
            return None
        state = mac_gate()
        return None if state["ok"] else str(state["reason"])

    def check_gate(self) -> None:
        """A Mac model stops (with the reason) when the Mac goes on battery or under
        memory pressure: checked every health interval, twice in a row to act."""
        if time.time() < self._next_gate:
            return
        self._next_gate = time.time() + HEALTH_EVERY
        reason = self.gate()
        self._gate_failures = self._gate_failures + 1 if reason else 0
        if self._gate_failures >= 2:
            raise Stop(f"stopped: {reason}")

    def prepare(self, deadline: float) -> None:
        """Make the model ready to answer. vLLM and the fake load at startup."""
        if self.spec["engine"] == "mlx":  # loaded on first use: do that now
            self.interruptible(
                self.http, "POST", "/v1/chat/completions",
                {"model": "default_model", "max_tokens": 1,
                 "messages": [{"role": "user", "content": "ready?"}]},
                timeout=max(5.0, deadline - time.time()),
            )  # fmt: skip
            return
        if self.spec["engine"] != "ollama":
            return
        model = self.spec["model"]
        remaining = max(5.0, deadline - time.time())
        self.interruptible(
            self.http, "POST", "/api/pull", {"model": model, "stream": False}, timeout=remaining
        )
        self.check_stop()
        tags = self.http("GET", "/api/tags", timeout=30).get("models") or []
        entry = self.named(tags)
        digest = entry.get("digest") if entry else None
        if not digest or not str(digest).startswith(self.spec["revision"]):
            if entry:  # don't keep gigabytes of a model nobody approved
                try:
                    self.http("DELETE", "/api/delete", {"model": model}, timeout=60)
                except (OSError, urllib.error.URLError, ValueError):
                    pass
            raise RuntimeError(
                f"{model} has digest {digest}, not the pinned {self.spec['revision']}: "
                "the tag moved since it was approved"
            )
        remaining = max(5.0, deadline - time.time())
        self.interruptible(
            self.http, "POST", "/api/generate", {"model": model, "keep_alive": -1},
            timeout=remaining,
        )  # fmt: skip

    def run(self) -> None:
        deadline = time.time() + float(self.spec["startup_timeout_s"])
        try:
            self.fetch(deadline)
            self.spawn()
            self.wait_up(deadline)
            self.update(state="loading")
            self.prepare(deadline)
            self.check_stop()
            self.check_gate()
            self.update(state="ready", ready_at=time.time(), healthy=True)
            self.watch()
        except Stop as stop:
            self.shutdown("stopped", stop.reason)
        except Exception as e:  # anything else is a failed service
            self.shutdown("failed", str(e) or type(e).__name__)

    def watch(self) -> None:
        failures = 0
        next_health = time.time() + HEALTH_EVERY
        while True:
            self.check_stop()
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(self.engine_error("the engine exited"))
            drain_until = self.drain_deadline()
            if drain_until is not None:
                if self.status["state"] != "draining":
                    self.update(state="draining", drain_until=drain_until)
                if time.time() >= drain_until:
                    raise Stop
            self.check_gate()
            if time.time() >= next_health:
                failures = 0 if self.healthy() else failures + 1
                healthy = failures < UNHEALTHY_AFTER
                if healthy != self.status.get("healthy"):
                    self.update(healthy=healthy)
                next_health = time.time() + HEALTH_EVERY
            time.sleep(POLL)

    def shutdown(self, state: str, error: Optional[str]) -> None:
        if state == "stopped":
            self.update(state="stopping")
        if self.proc is not None:
            # The whole group, even when the engine's main process already exited:
            # its children (ollama's runner, vLLM's workers) hold the model's memory.
            self.proc.poll()
            stop_group(self.proc.pid, self.engine_identity, grace=STOP_GRACE)
        self.update(state=state, error=error, finished_at=time.time(), healthy=None)


def main() -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)
    if len(sys.argv) != 2:
        sys.exit("usage: python -m newton_worker.run_service <service_dir>")
    service_dir = Path(sys.argv[1])
    supervisor = Supervisor(service_dir)
    if supervisor.status.get("state") != "starting":
        return  # someone else owns this service
    supervisor.run()


if __name__ == "__main__":
    main()
