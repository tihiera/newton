"""The SSH transport against real OpenSSH (opt-in: NEWTON_TEST_SSHD=1).

A user-level sshd on 127.0.0.1 with key-only auth and a ForceCommand that gives
each session a fake remote HOME. These cover what the fake harness can't: the
real KnownHostsCommand calls, ControlMaster/mux forwarding, how a refused
stream-local forward is reported, and hostile ~/.ssh/config entries.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, make_settings, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app

SSHD = "/usr/sbin/sshd"
pytestmark = pytest.mark.skipif(
    os.environ.get("NEWTON_TEST_SSHD") != "1" or not os.path.exists(SSHD),
    reason="set NEWTON_TEST_SSHD=1 (runs a user-level sshd on 127.0.0.1)",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class RealHost:
    def __init__(self, root: Path, **sshd_options: str) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.port = free_port()
        for name in ("hostkey", "clientkey"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)],
                check=True,
            )
        shutil.copy(root / "clientkey.pub", root / "authorized_keys")
        preamble = sshd_options.pop("preamble", "")
        options = {"AllowStreamLocalForwarding": "yes", **sshd_options}
        lines = [
            f"Port {self.port}",
            "ListenAddress 127.0.0.1",
            f"HostKey {root / 'hostkey'}",
            f"AuthorizedKeysFile {root / 'authorized_keys'}",
            f"PidFile {root / 'sshd.pid'}",
            "UsePAM no",
            "StrictModes no",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            *(f"{k} {v}" for k, v in options.items()),
            # Fake remote HOME; optional rc-file-style chatter before the command.
            f'ForceCommand HOME={self.home}; export HOME; cd "$HOME"; {preamble}'
            'exec /bin/sh -c "$SSH_ORIGINAL_COMMAND"',
        ]
        (root / "sshd_config").write_text("\n".join(lines) + "\n")
        subprocess.run(
            [SSHD, "-f", str(root / "sshd_config"), "-E", str(root / "sshd.log")], check=True
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    break
            time.sleep(0.1)

    def client_config(self, *extra: str) -> Path:
        path = self.root / "ssh_config"
        path.write_text(
            "\n".join(
                [
                    "Host spark",
                    "  HostName 127.0.0.1",
                    f"  Port {self.port}",
                    f"  User {os.environ.get('USER', 'nobody')}",
                    f"  IdentityFile {self.root / 'clientkey'}",
                    "  IdentitiesOnly yes",
                    *(f"  {line}" for line in extra),
                ]
            )
            + "\n"
        )
        wrapper = self.root / "ssh"
        wrapper.write_text(f'#!/bin/sh\nexec /usr/bin/ssh -F "{path}" "$@"\n')
        wrapper.chmod(0o755)
        return wrapper

    def stop(self) -> None:
        pid_file = self.root / "sshd.pid"
        if pid_file.exists():
            subprocess.run(["kill", pid_file.read_text().strip()], check=False)
        fp = self.home / ".newton"
        if (fp / "src").exists():
            subprocess.run(
                [sys.executable, "-m", "newton_worker", "--root", str(fp), "stop"],
                env={**os.environ, "PYTHONPATH": str(fp / "src")},
                capture_output=True,
                timeout=30,
            )


@pytest.fixture
def real_host_factory() -> Iterator[Any]:
    hosts: list[RealHost] = []

    def make(**sshd_options: str) -> RealHost:
        root = Path(tempfile.mkdtemp(prefix="nwsshd", dir="/tmp"))  # short: socket paths
        host = RealHost(root, **sshd_options)
        hosts.append(host)
        return host

    yield make
    for host in hosts:
        host.stop()
        shutil.rmtree(host.root, ignore_errors=True)


def app_for(host: RealHost, tmp_path: Path, *config: str) -> TestClient:
    settings = make_settings(tmp_path, ssh_executable=str(host.client_config(*config)))
    return TestClient(create_app(settings), headers=AUTH)


def add_host(client: TestClient) -> dict[str, Any]:
    body = {"name": "spark", "ssh_target": "spark", "python": sys.executable,
            "use_venv": False, "install_deps": False}  # fmt: skip
    host: dict[str, Any] = client.post("/hosts", json=body).json()
    return host


def connect(client: TestClient, host_id: str) -> Any:
    r = client.post(f"/hosts/{host_id}/connect")
    if r.status_code == 409:
        fps = [k["fingerprint"] for k in r.json()["fingerprints"]]
        assert all(k["type"] == "ssh-ed25519" for k in r.json()["fingerprints"])  # no "NONE"
        client.post(f"/hosts/{host_id}/hostkeys/trust", json={"fingerprints": fps})
        r = client.post(f"/hosts/{host_id}/connect")
    return r


def test_connect_and_run_over_real_openssh(real_host_factory: Any, tmp_path: Path) -> None:
    host = real_host_factory()
    with app_for(host, tmp_path) as client:
        spark = add_host(client)
        r = connect(client, spark["id"])
        assert r.status_code == 200, r.text
        job = wait_job(client, r.json()["selftest_job_id"], {"succeeded", "failed"}, 90)
        assert job["state"] == "succeeded", job
        runner = client.app.state.ctx.hosts._runners[spark["id"]]  # type: ignore[attr-defined]
        local_sock = Path(runner.tunnel.socket_path)
        assert local_sock.is_socket() and local_sock.stat().st_mode & 0o077 == 0
        assert local_sock.parent.stat().st_mode & 0o777 == 0o700


def test_refused_forwarding_is_reported_not_retried(real_host_factory: Any, tmp_path: Path) -> None:
    host = real_host_factory(AllowStreamLocalForwarding="no")
    with app_for(host, tmp_path) as client:
        spark = add_host(client)
        r = connect(client, spark["id"])
        assert r.status_code == 502
        assert r.json()["code"] == "forwarding_denied"
        assert client.post(f"/hosts/{spark['id']}/check").json()["code"] == "forwarding_denied"
        events = client.get(f"/events?entity_id={spark['id']}").json()
        assert sum(e["kind"] == "bootstrapped" for e in events) == 1  # no re-bootstrap loop


def test_users_local_forward_is_not_inherited(real_host_factory: Any, tmp_path: Path) -> None:
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    host = real_host_factory()
    try:
        with app_for(host, tmp_path, f"LocalForward 127.0.0.1:{port} 127.0.0.1:9") as client:
            spark = add_host(client)
            assert connect(client, spark["id"]).status_code == 200
    finally:
        busy.close()


def test_remote_command_alias_and_chatty_rc_files(real_host_factory: Any, tmp_path: Path) -> None:
    host = real_host_factory(preamble="echo 'Welcome to DGX OS'; ")
    with app_for(host, tmp_path, "RemoteCommand tmux attach", "RequestTTY yes") as client:
        spark = add_host(client)
        r = connect(client, spark["id"])
        assert r.status_code == 200, r.text
