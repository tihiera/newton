from __future__ import annotations

import io
import json
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from newton_worker.fsutil import UnsafePathError, safe_extract_tar_gz
from newton_worker.hardware import parse_nvidia_smi
from newton_worker.server import WorkerServer, serve

TOKEN = "test-token"


def tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Client:
    def __init__(self, port: int, token: str = TOKEN) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def call(self, method: str, path: str, body: Any = None, raw: bytes | None = None) -> Any:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            if resp.headers["Content-Type"] == "application/json":
                return json.loads(payload)
            return payload

    def wait(self, job_id: str, timeout: float = 20) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.call("GET", f"/jobs/{job_id}")
            if st["state"] in ("succeeded", "failed", "timed_out", "cancelled", "lost"):
                return st  # type: ignore[no-any-return]
            time.sleep(0.1)
        raise AssertionError(f"job {job_id} did not finish: {st}")


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[tuple[WorkerServer, Client]]:
    server = serve(tmp_path / "home", TOKEN)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield server, Client(server.server_address[1])
    server.shutdown()
    server.server_close()


def manifest(job_id: str, *args: str, timeout: int = 60) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "backend": "cpu",
        "benchmark": "selftest",
        "command": ["python", "-m", "newton_worker.selftest", *args],
        "timeout_seconds": timeout,
        "artifact_paths": ["results.json", "artifacts/"],
    }


def run_job(client: Client, m: dict[str, Any], bundle: bytes | None = None) -> dict[str, Any]:
    client.call("POST", "/jobs", m)
    client.call("PUT", f"/jobs/{m['job_id']}/bundle", raw=bundle or tar_gz({}))
    client.call("POST", f"/jobs/{m['job_id']}/start")
    return client.wait(m["job_id"])


def test_health_requires_token(worker: tuple[WorkerServer, Client]) -> None:
    server, client = worker
    assert client.call("GET", "/health")["ok"] is True
    with pytest.raises(urllib.error.HTTPError) as e:
        Client(server.server_address[1], token="wrong").call("GET", "/health")
    assert e.value.code == 401


def test_job_lifecycle_and_artifacts(worker: tuple[WorkerServer, Client], tmp_path: Path) -> None:
    _, client = worker
    st = run_job(client, manifest("job-1"))
    assert st["state"] == "succeeded", st
    assert st["exit_code"] == 0

    logs = client.call("GET", "/jobs/job-1/logs?stream=stdout")
    assert "selftest done" in logs["data"]
    assert logs["eof"] is True

    blob = client.call("GET", "/jobs/job-1/artifacts")
    dest = tmp_path / "out"
    safe_extract_tar_gz(blob, dest)
    assert (dest / "artifacts" / "hello.txt").read_text().startswith("hello")
    results = json.loads((dest / "results.json").read_text())
    assert results["metrics"]["checksum"] > 0


def test_create_is_idempotent(worker: tuple[WorkerServer, Client]) -> None:
    _, client = worker
    m = manifest("job-idem")
    a = client.call("POST", "/jobs", m)
    b = client.call("POST", "/jobs", m)
    assert a["created_at"] == b["created_at"]
    with pytest.raises(urllib.error.HTTPError) as e:
        client.call("POST", "/jobs", {**m, "timeout_seconds": 5})
    assert e.value.code == 409


def test_failure_and_timeout_and_cancel(worker: tuple[WorkerServer, Client]) -> None:
    _, client = worker
    assert run_job(client, manifest("job-fail", "--fail"))["state"] == "failed"
    assert run_job(client, manifest("job-to", "--sleep", "30", timeout=1))["state"] == "timed_out"

    m = manifest("job-cancel", "--sleep", "30")
    client.call("POST", "/jobs", m)
    client.call("PUT", "/jobs/job-cancel/bundle", raw=tar_gz({}))
    client.call("POST", "/jobs/job-cancel/start")
    time.sleep(0.5)
    client.call("POST", "/jobs/job-cancel/cancel")
    assert client.wait("job-cancel")["state"] == "cancelled"


def test_job_survives_worker_restart(tmp_path: Path) -> None:
    root = tmp_path / "home"
    server = serve(root, TOKEN)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    client = Client(server.server_address[1])
    m = manifest("job-restart", "--sleep", "1.5")
    client.call("POST", "/jobs", m)
    client.call("PUT", "/jobs/job-restart/bundle", raw=tar_gz({}))
    client.call("POST", "/jobs/job-restart/start")
    server.shutdown()
    server.server_close()

    server2 = serve(root, TOKEN)
    t2 = threading.Thread(target=server2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t2.start()
    try:
        assert Client(server2.server_address[1]).wait("job-restart")["state"] == "succeeded"
    finally:
        server2.shutdown()
        server2.server_close()


@pytest.mark.parametrize(
    "command",
    [
        ["bash", "-c", "echo hi"],
        ["python", "-c", "print(1)"],
        ["python", "-m", "os"],
        ["python", "-m", "benchmarks_evil"],
    ],
)
def test_command_policy(worker: tuple[WorkerServer, Client], command: list[str]) -> None:
    _, client = worker
    with pytest.raises(urllib.error.HTTPError) as e:
        client.call("POST", "/jobs", {**manifest("job-bad"), "command": command})
    assert e.value.code in (400, 403)


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/passwd", "a/../../b"])
def test_bundle_path_traversal_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_extract_tar_gz(tar_gz({name: b"x"}), tmp_path / "dest")


def test_bundle_symlink_rejected(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(UnsafePathError):
        safe_extract_tar_gz(buf.getvalue(), tmp_path / "dest")


def test_parse_nvidia_smi() -> None:
    out = "0, NVIDIA A100-SXM4-80GB, 81920, 80000, 550.54.15, 3\n"
    gpus = parse_nvidia_smi(out)
    assert gpus[0]["name"] == "NVIDIA A100-SXM4-80GB"
    assert gpus[0]["memory_free_mib"] == 80000


def _unix_get(sock_path: str, path: str, token: str = TOKEN) -> tuple[int, Any]:
    import http.client
    import socket as socketlib

    class UnixConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
            self.sock.connect(sock_path)

    conn = UnixConnection("localhost", timeout=10)
    conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    return resp.status, body


def test_unix_socket_server(tmp_path: Path) -> None:
    import tempfile

    short = Path(tempfile.mkdtemp(prefix="nw", dir="/tmp"))
    try:
        sock = short / "worker.sock"
        sock.write_text("")  # stale file left by a crashed worker
        server = serve(tmp_path / "home", TOKEN, socket_path=sock)
        t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        t.daemon = True
        t.start()
        try:
            assert sock.is_socket()
            assert sock.stat().st_mode & 0o077 == 0  # owner only
            status, body = _unix_get(str(sock), "/health")
            assert status == 200 and body["ok"] is True
            assert _unix_get(str(sock), "/health", token="wrong")[0] == 401
            info = json.loads((tmp_path / "home" / "worker.json").read_text())
            assert info["socket"] == str(sock) and info["port"] is None
            with pytest.raises(OSError, match="already serving"):
                serve(tmp_path / "home2", TOKEN, socket_path=sock)  # never steal a live one
        finally:
            server.shutdown()
            server.server_close()
    finally:
        import shutil

        shutil.rmtree(short, ignore_errors=True)


@pytest.mark.parametrize("backend", ["cuda", "gpu", "metal"])
def test_worker_refuses_backends_it_cannot_run(
    worker: tuple[WorkerServer, Client], backend: str
) -> None:
    import shutil as _shutil
    import sys as _sys

    if backend in ("cuda", "gpu") and _shutil.which("nvidia-smi"):
        pytest.skip("this machine has an NVIDIA driver")
    if backend == "metal" and _sys.platform == "darwin":
        try:
            import importlib.metadata as md

            md.version("mlx")
            pytest.skip("MLX is installed here")
        except md.PackageNotFoundError:
            pass
    _, client = worker
    with pytest.raises(urllib.error.HTTPError) as e:
        client.call("POST", "/jobs", {**manifest(f"job-{backend}"), "backend": backend})
    assert e.value.code == 422
    assert "not available on this worker" in json.loads(e.value.read())["error"]


def test_parse_nvidia_smi_gb10_unified_memory() -> None:
    gpus = parse_nvidia_smi("0, NVIDIA GB10, [N/A], [N/A], 580.173.02, 0\n")
    assert gpus[0]["name"] == "NVIDIA GB10"
    assert gpus[0]["memory_total_mib"] is None
    assert gpus[0]["unified_memory"] is True


def test_preflight_explains_each_missing_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    from newton_worker import jobs as worker_jobs

    monkeypatch.setattr(worker_jobs.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(worker_jobs, "_installed", lambda dist: False)
    assert "CuPy is not installed" in (worker_jobs.backend_unavailable("cuda") or "")
    monkeypatch.setattr(worker_jobs, "_installed", lambda dist: dist == "cupy-cuda13x")
    assert worker_jobs.backend_unavailable("cuda") is None
    monkeypatch.setattr(worker_jobs.sys, "platform", "linux")
    assert "not an Apple Silicon Mac" in (worker_jobs.backend_unavailable("metal") or "")


def test_a_failing_nvidia_smi_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newton_worker.hardware import probe_gpus

    fake = tmp_path / "nvidia-smi"
    fake.write_text(
        "#!/bin/sh\necho 'Failed to initialize NVML: Driver/library version mismatch'\nexit 9\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    gpus, error = probe_gpus()
    assert gpus == [] and error is not None and "Driver/library version mismatch" in error


def test_tokens_from_every_install_are_accepted(tmp_path: Path) -> None:
    from newton_worker.server import Tokens

    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "aaaa").write_text("token-a")
    tokens = Tokens(root=tmp_path)
    assert tokens.check("token-a") and not tokens.check("token-b")
    (tmp_path / "tokens" / "bbbb").write_text("token-b")  # a second Mac installs itself
    assert tokens.check("token-b") and tokens.check("token-a")
    assert not tokens.check("")


def test_worker_reports_the_code_it_loaded_and_cleans_up(tmp_path: Path) -> None:
    import os as _os
    import shutil as _shutil
    import signal as _signal
    import subprocess as _subprocess
    import sys as _sys
    import tempfile

    from newton_worker.__main__ import is_worker_process

    short = Path(tempfile.mkdtemp(prefix="nw", dir="/tmp"))
    try:
        src = short / "src"
        _shutil.copytree(
            Path(__file__).resolve().parents[1] / "newton_worker",
            src / "newton_worker",
            ignore=_shutil.ignore_patterns("__pycache__"),
        )
        (src / "SOURCE_DIGEST").write_text("a" * 64 + "\n")
        root = short / "home"
        sock = short / "w.sock"
        proc = _subprocess.Popen(
            [_sys.executable, "-m", "newton_worker", "--root", str(root), "serve",
             "--socket", str(sock)],
            env={**_os.environ, "PYTHONPATH": str(src), "NEWTON_WORKER_TOKEN": TOKEN},
            stdout=_subprocess.DEVNULL,
        )  # fmt: skip
        deadline = time.time() + 10
        while not sock.exists() and time.time() < deadline:
            time.sleep(0.05)
        (src / "SOURCE_DIGEST").write_text("b" * 64 + "\n")  # an upgrade lands on disk
        assert _unix_get(str(sock), "/health")[1]["source_digest"] == "a" * 64
        assert is_worker_process(proc.pid, root)
        assert not is_worker_process(proc.pid, short / "someone-elses-root")
        proc.send_signal(_signal.SIGTERM)
        proc.wait(timeout=10)
        assert not (root / "worker.json").exists() and not sock.exists()
    finally:
        _shutil.rmtree(short, ignore_errors=True)
