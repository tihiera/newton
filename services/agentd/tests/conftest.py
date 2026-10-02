from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings

# Shared simulated-host Python environments (fake wheels, distro-python shims).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker" / "tests"))
from pyenv_shims import offline_index_env  # noqa: E402

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FAKESSH = Path(__file__).with_name("fakessh.py")
HOSTKEY = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 36).decode()


def make_settings(root: Path, **overrides: Any) -> Settings:
    s = Settings(
        data_dir=root / "data",
        api_token=TOKEN,
        secret_backend="memory",
        known_hosts_path=root / "known_hosts",
        scheduler_interval=0.1,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), headers=AUTH) as c:
        yield c


def wait_for(
    fn: Callable[[], Any], until: Callable[[Any], bool], timeout: float = 60, interval: float = 0.1
) -> Any:
    deadline = time.time() + timeout
    value = fn()
    while not until(value):
        if time.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s; last value: {value}")
        time.sleep(interval)
        value = fn()
    return value


def wait_job(client: TestClient, job_id: str, states: set[str], timeout: float = 60) -> Any:
    return wait_for(
        lambda: client.get(f"/jobs/{job_id}").json(), lambda j: j["state"] in states, timeout
    )


class FakeRemote:
    """A fake Linux box reachable through the fake ssh executables."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Short path: the worker's Unix socket must fit in ~104 bytes.
        self.short = Path(tempfile.mkdtemp(prefix="nw", dir="/tmp"))
        self.home = self.short / "home"
        self.home.mkdir()
        self.fp = self.home / ".newton"
        bindir = root / "bin"
        bindir.mkdir()
        self.ssh = bindir / "ssh"
        self.keyscan = bindir / "ssh-keyscan"
        for path, mode in ((self.ssh, "ssh"), (self.keyscan, "keyscan")):
            path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKESSH}" {mode} "$@"\n')
            path.chmod(0o755)
        monkeypatch.setenv("FAKESSH_REMOTE_HOME", str(self.home))
        monkeypatch.setenv("FAKESSH_HOSTKEY", HOSTKEY)
        # Hermetic by default: no test may reach a real package index.
        empty = root / "no-wheels"
        empty.mkdir()
        for key, value in offline_index_env(empty, root / "uv-cache").items():
            monkeypatch.setenv(key, value)

    def _worker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "newton_worker", "--root", str(self.fp), *args],
            env={**os.environ, "PYTHONPATH": str(self.fp / "src")},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def worker_pid(self) -> int | None:
        info = json.loads(self._worker("status").stdout or "{}")
        return info.get("pid") if info.get("running") else None

    def stop_worker(self) -> None:
        if (self.fp / "src").exists():
            self._worker("stop")

    def restart_worker_as_stale(self, digest: str = "0" * 64) -> int:
        """Restart the worker so the *process* carries an old source digest
        (the digest is read once at startup, like real stale code)."""
        self.stop_worker()
        (self.fp / "src" / "SOURCE_DIGEST").write_text(digest + "\n")
        (self.fp / "worker.json").unlink(missing_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-m", "newton_worker", "--root", str(self.fp), "serve",
             "--socket", str(self.fp / "worker.sock")],
            env={**os.environ, "PYTHONPATH": str(self.fp / "src")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )  # fmt: skip
        wait_for(lambda: (self.fp / "worker.json").exists(), bool, timeout=20)
        return proc.pid


@pytest.fixture
def fake_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRemote]:
    remote = FakeRemote(tmp_path, monkeypatch)
    yield remote
    remote.stop_worker()
    shutil.rmtree(remote.short, ignore_errors=True)


@pytest.fixture
def ssh_settings(tmp_path: Path, fake_remote: FakeRemote) -> Settings:
    return make_settings(
        tmp_path,
        ssh_executable=str(fake_remote.ssh),
        ssh_keyscan_executable=str(fake_remote.keyscan),
    )
