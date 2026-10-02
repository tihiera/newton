"""Step 1 (G1) through the API over the fake SSH: a DGX-OS-like Python gets a
working venv, failures surface as specific codes, stale workers upgrade
themselves, and a failed upgrade never takes a working host down."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, FakeRemote, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.runners.base import RunnerError
from newton_agentd.runners.ssh import SshRunner, worker_source_digest
from pyenv_shims import (  # noqa: F401 - fixtures
    FAKE_NUMPY,
    base_python,
    empty_wheelhouse,
    make_python_shim,
    tool_path,
    wheelhouse,
)


@pytest.fixture
def with_wheels(fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch, wheelhouse: Path) -> None:
    # Depends on fake_remote so it runs after (and overrides) its offline default.
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    monkeypatch.setenv("UV_FIND_LINKS", str(wheelhouse))


@pytest.fixture
def host_tools(monkeypatch: pytest.MonkeyPatch, tool_path: str) -> None:
    monkeypatch.setenv("FAKESSH_REMOTE_PATH", tool_path)


def add_trusted_host(client: TestClient, python: Path | str, **extra: Any) -> dict[str, Any]:
    r = client.post(
        "/hosts",
        json={
            "name": "spark",
            "ssh_target": "spark",
            "python": str(python),
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    host: dict[str, Any] = r.json()
    keys = client.get(f"/hosts/{host['id']}/hostkeys").json()
    client.post(
        f"/hosts/{host['id']}/hostkeys/trust",
        json={"fingerprints": [k["fingerprint"] for k in keys]},
    )
    return host


def runner_of(client: TestClient, host_id: str) -> SshRunner:
    runner = client.app.state.ctx.hosts._runners[host_id]  # type: ignore[attr-defined]
    assert isinstance(runner, SshRunner)
    return runner


def drop_tunnel(client: TestClient, host_id: str) -> None:
    """What a Wi-Fi change or laptop sleep does: the next call reconnects."""
    runner = runner_of(client, host_id)
    assert runner.tunnel is not None and runner.tunnel.proc is not None
    runner.tunnel.proc.kill()


def host_events(client: TestClient, host_id: str, kind: str) -> list[dict[str, Any]]:
    events = client.get(f"/events?entity_id={host_id}").json()
    return [e["data"] for e in events if e["kind"] == kind]


@pytest.mark.usefixtures("with_wheels", "host_tools")
def test_dgx_like_python_gets_a_working_venv(
    ssh_settings: Settings, fake_remote: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    # No python3-venv (no ensurepip) and PEP 668, like a stock DGX OS / Ubuntu 24.04.
    py = make_python_shim(
        tmp_path / "remote-bin" / "python3", base_python, no_ensurepip=True, pep668=True
    )
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, py)
        r = client.post(f"/hosts/{host['id']}/bootstrap")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "online"
        hw = r.json()["hardware"]
        assert hw["packages"]["numpy"] == FAKE_NUMPY
        assert hw["bootstrap"]["venv"] == "created"
        assert hw["bootstrap"]["installer"] == "system-pip"
        assert hw["python_executable"] == str(fake_remote.fp / "venv/bin/python")
        assert hw["worker"]["source_digest"] == worker_source_digest(ssh_settings.worker_source_dir)
        # Private on shared hosts: other users must not read jobs, logs or the token.
        assert fake_remote.fp.stat().st_mode & 0o777 == 0o700
        assert all(t.stat().st_mode & 0o777 == 0o600 for t in (fake_remote.fp / "tokens").iterdir())
        assert (fake_remote.fp / "src").stat().st_mode & 0o077 == 0
        assert not list(fake_remote.fp.glob("src.stage-*"))  # staging dir swapped in


@pytest.mark.usefixtures("host_tools")
@pytest.mark.parametrize(
    "modes", [{"no_venv": True, "no_pip": True}, {"no_ensurepip": True, "no_pip": True}]
)
def test_host_without_venv_or_pip_reports_venv_unavailable(
    ssh_settings: Settings,
    fake_remote: FakeRemote,
    base_python: Path,
    tmp_path: Path,
    modes: dict[str, bool],
) -> None:
    py = make_python_shim(tmp_path / "remote-bin" / "python3", base_python, **modes)
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, py)
        r = client.post(f"/hosts/{host['id']}/bootstrap")
        assert r.status_code == 502
        assert r.json()["code"] == "venv_unavailable"
        assert "sudo apt install python3-venv" in r.json()["error"]
        assert client.get(f"/hosts/{host['id']}").json()["status"] == "error:venv_unavailable"
        assert not (fake_remote.fp / "worker.json").exists()


@pytest.mark.usefixtures("host_tools")
def test_failed_dependency_install_reports_deps_missing(
    ssh_settings: Settings, fake_remote: FakeRemote, base_python: Path
) -> None:
    # The fake remote is offline by default: nothing to install numpy from.
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, base_python)
        r = client.post(f"/hosts/{host['id']}/bootstrap")
        assert r.status_code == 502
        assert r.json()["code"] == "deps_missing"


def test_busy_host_is_a_transient_error(
    ssh_settings: Settings, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = fake_remote.fp / "bootstrap.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    monkeypatch.setenv("NEWTON_BOOTSTRAP_LOCK_WAIT", "1")
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False)
        r = client.post(f"/hosts/{host['id']}/bootstrap")
        assert r.status_code == 502
        assert r.json()["code"] == "bootstrap_busy"


def test_stale_worker_is_upgraded_on_reconnect(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False)
        assert client.post(f"/hosts/{host['id']}/bootstrap").json()["status"] == "online"

        # The host still runs last week's worker: a process that loaded old code.
        stale_pid = fake_remote.restart_worker_as_stale()
        drop_tunnel(client, host["id"])

        r = client.post(f"/hosts/{host['id']}/check")
        assert r.status_code == 200, r.text
        after = r.json()
        assert after["status"] == "online"
        expected = worker_source_digest(ssh_settings.worker_source_dir)
        assert after["hardware"]["worker"]["source_digest"] == expected
        assert after["hardware"]["worker"]["pid"] != stale_pid  # really restarted
        assert host_events(client, host["id"], "worker_upgraded") == [
            {"from": "0" * 64, "to": expected}
        ]
        # The upgrade honoured the host's settings: no venv, nothing installed.
        assert after["hardware"]["bootstrap"]["venv"] == "none"
        assert not (fake_remote.fp / "venv").exists()


@pytest.mark.usefixtures("with_wheels", "host_tools")
def test_failed_upgrade_keeps_the_old_worker_and_backs_off(
    ssh_settings: Settings,
    fake_remote: FakeRemote,
    base_python: Path,
    empty_wheelhouse: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, base_python)
        assert client.post(f"/hosts/{host['id']}/bootstrap").json()["status"] == "online"
        stale_pid = fake_remote.restart_worker_as_stale()

        # Make the upgrade fail: it must rebuild the venv but the index is gone.
        cfg = fake_remote.fp / "venv" / "pyvenv.cfg"
        cfg.write_text(cfg.read_text().replace("= false", "= true"))
        monkeypatch.setenv("PIP_FIND_LINKS", str(empty_wheelhouse))

        for _ in range(2):  # the second reconnect must not retry (backoff)
            drop_tunnel(client, host["id"])
            after = client.post(f"/hosts/{host['id']}/check").json()
            assert after["status"] == "online"  # served by the old worker
            assert after["hardware"]["worker"]["pid"] == stale_pid
            assert "upgrade pending (deps_missing)" in after["last_error"]
        failures = host_events(client, host["id"], "worker_upgrade_failed")
        assert len(failures) == 1
        assert failures[0]["code"] == "deps_missing"
        # The failed upgrade left the live source alone (it only ever lands on success).
        assert (fake_remote.fp / "src" / "SOURCE_DIGEST").read_text().strip() == "0" * 64

        # Same protocol version: the old worker keeps running new jobs meanwhile.
        job = client.post(f"/hosts/{host['id']}/selftest", json={}).json()
        done = wait_job(client, job["id"], {"succeeded", "failed", "cancelled"}, timeout=60)
        assert done["state"] == "succeeded", done


def test_csh_login_shell(
    ssh_settings: Settings, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    csh = shutil.which("csh") or shutil.which("tcsh")
    if csh is None:
        pytest.skip("no csh/tcsh on this machine")
    monkeypatch.setenv("FAKESSH_LOGIN_SHELL", csh)
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False)
        r = client.post(f"/hosts/{host['id']}/bootstrap")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "online"


def test_failed_restart_backs_off(
    ssh_settings: Settings, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False)
        assert client.post(f"/hosts/{host['id']}/bootstrap").json()["status"] == "online"
        # The worker dies, and restarting it fails (another bootstrap holds the lock).
        fake_remote.stop_worker()
        lock = fake_remote.fp / "bootstrap.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))
        monkeypatch.setenv("NEWTON_BOOTSTRAP_LOCK_WAIT", "1")
        r = client.post(f"/hosts/{host['id']}/check")
        assert r.status_code == 502 and r.json()["code"] == "bootstrap_busy"
        runner = runner_of(client, host["id"])
        for _ in range(3):  # the scheduler polling meanwhile: no new bootstrap attempts
            with pytest.raises(RunnerError) as e:
                client.portal.call(runner.status, "some-job")  # type: ignore[union-attr]
            assert e.value.code == "bootstrap_busy"
        assert len(host_events(client, host["id"], "worker_restart_failed")) == 1
        # An explicit check means "try again now" (e.g. the user fixed the host).
        assert client.post(f"/hosts/{host['id']}/check").status_code == 502
        assert len(host_events(client, host["id"], "worker_restart_failed")) == 2


def drop_tunnel_if_any(client: TestClient, host_id: str) -> None:
    runner = client.app.state.ctx.hosts._runners.get(host_id)  # type: ignore[attr-defined]
    if runner is not None and runner.tunnel is not None and runner.tunnel.proc is not None:
        runner.tunnel.proc.kill()


def test_host_misreporting_its_digest_is_not_counted_as_upgraded(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    from newton_agentd.runners.ssh import WorkerSource

    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False)
        assert client.post(f"/hosts/{host['id']}/bootstrap").json()["status"] == "online"
        runner = runner_of(client, host["id"])
        # agentd expects a digest the uploaded code will never report.
        runner.source = WorkerSource(digest="f" * 64, tarball=runner.source.tarball)
        for _ in range(2):
            drop_tunnel(client, host["id"])
            assert client.post(f"/hosts/{host['id']}/check").json()["status"] == "online"
        assert host_events(client, host["id"], "worker_upgraded") == []
        failures = host_events(client, host["id"], "worker_upgrade_failed")
        assert [f["code"] for f in failures] == ["upgrade_unverified"]  # once, then backoff


@pytest.mark.usefixtures("with_wheels", "host_tools")
def test_failed_upgrade_is_retried_when_the_backoff_expires(
    ssh_settings: Settings,
    fake_remote: FakeRemote,
    base_python: Path,
    empty_wheelhouse: Path,
    wheelhouse: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, base_python)
        assert client.post(f"/hosts/{host['id']}/bootstrap").json()["status"] == "online"
        stale_pid = fake_remote.restart_worker_as_stale()
        cfg = fake_remote.fp / "venv" / "pyvenv.cfg"
        cfg.write_text(cfg.read_text().replace("= false", "= true"))
        monkeypatch.setenv("PIP_FIND_LINKS", str(empty_wheelhouse))
        drop_tunnel(client, host["id"])
        assert "upgrade pending" in client.post(f"/hosts/{host['id']}/check").json()["last_error"]

        # The index is back and the backoff expires -- with the tunnel up the whole time.
        monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
        runner = runner_of(client, host["id"])
        assert runner.tunnel is not None and runner.tunnel.alive
        runner._upgrade_retry_at = 0.0
        job = client.post(f"/hosts/{host['id']}/selftest", json={}).json()
        done = wait_job(client, job["id"], {"succeeded", "failed", "cancelled"}, timeout=90)
        assert done["state"] == "succeeded", done
        after = client.post(f"/hosts/{host['id']}/check").json()
        assert after["last_error"] is None  # no longer "upgrade pending"
        upgraded = host_events(client, host["id"], "worker_upgraded")
        assert len(upgraded) == 1
        info = fake_remote.worker_pid()
        assert info is not None and info != stale_pid


def test_force_cancel_and_force_delete_for_a_host_that_is_gone(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, "python3")
        ctx = client.app.state.ctx  # type: ignore[attr-defined]
        from newton_agentd.orchestration.jobs import create_job, selftest_manifest
        from newton_agentd.runners.bundle import build_bundle

        ids = [
            create_job(
                ctx.db, ctx.settings, host_id=host["id"], role="selftest", label=f"j{i}",
                manifest=selftest_manifest(), bundle=build_bundle(None, {}), state="running",
            )
            for i in range(2)
        ]  # fmt: skip
        r = client.delete(f"/hosts/{host['id']}")
        assert r.status_code == 400 and "force=true" in r.json()["error"]
        forced = client.post(f"/jobs/{ids[0]}/cancel?force=true").json()
        assert forced["state"] == "cancelled" and "remote state unknown" in forced["error"]
        assert client.delete(f"/hosts/{host['id']}?force=true").status_code == 204
        assert client.get(f"/jobs/{ids[1]}").json()["state"] == "cancelled"
        assert host["id"] not in [h["id"] for h in client.get("/hosts").json()]
        # History survives, and the name is free for a new host.
        assert client.get(f"/jobs/{ids[0]}").status_code == 200
        again = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"})
        assert again.status_code == 201
