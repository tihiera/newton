"""B1 over SSH, against the fake-ssh harness (see fakessh.py)."""

from __future__ import annotations

import sys
from typing import Any

from conftest import AUTH, FakeRemote, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.runners.ssh import SshRunner

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}


def add_host(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/hosts",
        json={
            "name": "gpu-box",
            "ssh_target": "gpu-box",
            "python": sys.executable,
            "use_venv": False,
            "install_deps": False,
        },
    )
    assert r.status_code == 201, r.text
    host: dict[str, Any] = r.json()
    assert "token_ref" not in host
    return host


def trust(client: TestClient, host_id: str) -> None:
    keys = client.get(f"/hosts/{host_id}/hostkeys").json()
    assert keys and keys[0]["fingerprint"].startswith("SHA256:")
    trusted = client.post(
        f"/hosts/{host_id}/hostkeys/trust", json={"fingerprints": [keys[0]["fingerprint"]]}
    ).json()
    assert trusted == keys


def test_unknown_host_key_is_refused(ssh_settings: Settings, fake_remote: FakeRemote) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        r = client.post(f"/hosts/{host['id']}/bootstrap")
        assert r.status_code == 502
        assert r.json()["code"] == "hostkey_unknown"
        assert client.get(f"/hosts/{host['id']}").json()["status"] == "error:hostkey_unknown"
        bad = client.post(
            f"/hosts/{host['id']}/hostkeys/trust", json={"fingerprints": ["SHA256:nope"]}
        )
        assert bad.status_code == 502


def test_bootstrap_tunnel_job_and_artifacts(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        trust(client, host["id"])

        online = client.post(f"/hosts/{host['id']}/bootstrap").json()
        assert online["status"] == "online", online
        assert online["hardware"]["worker"]["ok"] is True
        (token_file,) = (fake_remote.home / ".newton" / "tokens").iterdir()  # one per install
        assert token_file.stat().st_mode & 0o777 == 0o600
        assert token_file.parent.stat().st_mode & 0o777 == 0o700

        job = client.post(f"/hosts/{host['id']}/selftest", json={}).json()
        done = wait_job(client, job["id"], TERMINAL)
        assert done["state"] == "succeeded", done
        hello = client.get(f"/jobs/{job['id']}/artifacts/artifacts/hello.txt").text
        assert hello.startswith("hello from the newton worker")
        # The job really ran in the "remote" home, not locally.
        assert (fake_remote.home / ".newton" / "jobs" / f"{job['id']}-a1").is_dir()


def test_tunnel_and_worker_self_heal(ssh_settings: Settings, fake_remote: FakeRemote) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        trust(client, host["id"])
        client.post(f"/hosts/{host['id']}/bootstrap")

        ctx = client.app.state.ctx  # type: ignore[attr-defined]
        runner = ctx.hosts._runners[host["id"]]
        assert isinstance(runner, SshRunner)

        # Tunnel drops (laptop sleeps, Wi-Fi changes): next call reconnects.
        assert runner.tunnel is not None and runner.tunnel.proc is not None
        runner.tunnel.proc.kill()
        assert client.post(f"/hosts/{host['id']}/check").json()["status"] == "online"

        # Remote worker dies (reboot): the runner restarts it over SSH.
        fake_remote.stop_worker()
        assert client.post(f"/hosts/{host['id']}/check").json()["status"] == "online"


def test_same_experiment_runs_over_ssh(ssh_settings: Settings, fake_remote: FakeRemote) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        trust(client, host["id"])
        client.post(f"/hosts/{host['id']}/bootstrap")
        small = {"resolutions": [32, 64], "repeats": 1}
        exp = client.post(
            "/experiments",
            json={
                "title": "ssh run",
                "host_id": host["id"],
                "variants": [
                    {
                        "role": "baseline",
                        "label": "upwind",
                        "params": {"scheme": "upwind", **small},
                    },
                    {
                        "role": "candidate",
                        "label": "vanleer",
                        "params": {"scheme": "muscl_vanleer", **small},
                    },
                ],  # fmt: skip
            },
        ).json()
        approval = client.get("/approvals?status=pending").json()[0]
        client.post(f"/approvals/{approval['id']}/approve")
        for j in exp["jobs"]:
            assert wait_job(client, j["id"], TERMINAL, timeout=120)["state"] == "succeeded"
