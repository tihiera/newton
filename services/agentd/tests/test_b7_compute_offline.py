"""B7, compute while offline: a host that can't be reached (down, asleep, its name not
resolving because this Mac is offline) never fails a queued job or an approved model
service. They wait for it with backoff and say why, the host is checked again by
itself, and the work goes on as soon as the host is back."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, FakeRemote, make_settings, wait_for, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.orchestration import hosts as hosts_mod
from newton_agentd.orchestration import scheduler as scheduler_mod
from newton_agentd.orchestration.jobs import create_job, selftest_manifest
from newton_agentd.runners.base import RunnerError
from newton_agentd.runners.bundle import build_bundle
from newton_agentd.runners.ssh import classify_ssh_error
from newton_agentd.serving import manager as manager_mod
from newton_agentd.storage.db import dumps
from test_bootstrap_api import add_trusted_host, drop_tunnel
from test_services_api import create, stop_services, wait_state

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}
RESOLVE = "ssh: Could not resolve hostname spark: nodename nor servname provided, or not known"


def unresolvable() -> RunnerError:
    # What an older agentd raised as permanent (transient=False): still a wait.
    return RunnerError(f"ssh: {RESOLVE}", transient=False, code="unresolvable")


def ctx_of(client: TestClient) -> Any:
    return client.app.state.ctx  # type: ignore[attr-defined]


# -- classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (RESOLVE, ("unresolvable", True)),
        ("ssh: connect to host 10.0.0.7 port 22: Network is unreachable", ("unreachable", True)),
        ("ssh: connect to host spark port 22: Host is down", ("unreachable", True)),
        ("ssh: connect to host spark port 22: Operation timed out", ("unreachable", True)),
        # Also what a failing ProxyCommand/ProxyJump prints: a config problem, not a down host.
        ("kex_exchange_identification: read: Connection reset by peer", ("ssh_error", True)),
        # ssh's own trouble, not the host's: the bounded retry, then the job fails.
        ("/Users/me/.ssh/config: line 3: Bad configuration option: foo", ("ssh_error", True)),
        (
            "Received disconnect from 10.0.0.7 port 22:2: Too many authentication failures",
            ("ssh_error", True),
        ),  # fmt: skip
        ("Permission denied (publickey).", ("auth_failed", False)),
        ("Host key verification failed.", ("hostkey_unknown", False)),
    ],
)
def test_reachability_errors_are_transient(stderr: str, expected: tuple[str, bool]) -> None:
    assert classify_ssh_error(stderr) == expected


def test_the_waiting_sentence_names_the_host_and_the_reason() -> None:
    reason = hosts_mod.down_reason(unresolvable())
    assert reason.startswith("its name doesn't resolve; is this Mac offline?")
    assert "(Could not resolve hostname spark" in reason  # ssh's own words, once
    assert "ssh:" not in reason
    assert hosts_mod.host_down(unresolvable(), "ssh")
    assert not hosts_mod.host_down(RunnerError("no", code="auth_failed", transient=False), "ssh")
    assert not hosts_mod.host_down(unresolvable(), "local")  # this Mac is never "down"
    config = RunnerError("ssh: Bad configuration option: foo", code="ssh_error")
    assert not hosts_mod.host_down(config, "ssh")  # nothing to wait for: the user fixes it


# -- a scripted host ----------------------------------------------------------------------


class DownableRunner:
    """A worker host that is down (every call raises `down`) until the test says so."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self.down: RunnerError | None = unresolvable()
        self.submits = 0
        self.checks = 0
        self.state = "succeeded"

    def _gate(self) -> None:
        if self.down is not None:
            raise self.down

    async def ensure_ready(self) -> None:
        self._gate()

    async def submit(self, manifest: dict[str, Any], bundle: bytes) -> dict[str, Any]:
        self.submits += 1
        self._gate()
        return {"state": "running"}

    async def status(self, remote_id: str) -> dict[str, Any]:
        self._gate()
        return {"job_id": remote_id, "state": self.state}

    async def logs(self, remote_id: str, stream: str, offset: int) -> dict[str, Any]:
        self._gate()
        return {"data": "", "next_offset": offset}

    async def artifacts(self, remote_id: str) -> bytes:
        self._gate()
        return build_bundle(None, {"results.json": {"metrics": {"checksum": 1}}})

    async def cancel(self, remote_id: str) -> dict[str, Any]:
        self._gate()
        return {"state": "cancelled"}

    async def health(self) -> dict[str, Any]:
        self.checks += 1
        self._gate()
        return {"ok": True}

    async def hardware(self) -> dict[str, Any]:
        self._gate()
        return {"gpus": []}

    async def close(self) -> None:
        pass


def add_host(client: TestClient, name: str, status: str, hardware: Any) -> str:
    host_id: str = client.post("/hosts", json={"name": name, "ssh_target": name}).json()["id"]
    ctx_of(client).hosts._update(
        host_id, status=status, hardware=None if hardware is None else dumps(hardware)
    )
    return host_id


def use_runner(client: TestClient, runner: DownableRunner) -> None:
    hosts = ctx_of(client).hosts
    original = hosts.runner

    async def pick(host_id: str) -> Any:
        return runner if host_id == runner.host_id else await original(host_id)

    hosts.runner = pick


def add_job(client: TestClient, host_id: str) -> str:
    ctx = ctx_of(client)
    job_id = create_job(ctx.db, ctx.settings, host_id=host_id, role="selftest",
                        label="selftest", manifest=selftest_manifest(),
                        bundle=build_bundle(None, {}))  # fmt: skip
    ctx.scheduler.wake()
    return job_id


@pytest.fixture
def down(client: TestClient) -> DownableRunner:
    """An SSH host that was online once (it has hardware) and now can't be reached."""
    runner = DownableRunner(add_host(client, "spark", "online", {"gpus": []}))
    use_runner(client, runner)
    return runner


# -- queued jobs wait -----------------------------------------------------------------------


def test_a_queued_job_waits_for_a_host_that_does_not_resolve(
    client: TestClient, down: DownableRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_mod, "HOST_WAIT_START", 0.05)
    monkeypatch.setattr(scheduler_mod, "HOST_WAIT_MAX", 0.2)
    monkeypatch.setattr(hosts_mod, "RECHECK_START", 3600.0)  # only the job's own retries
    job_id = add_job(client, down.host_id)
    max_attempts = ctx_of(client).settings.max_submit_attempts
    wait_for(lambda: down.submits, lambda n: n > max_attempts + 3, timeout=30)
    job = client.get(f"/jobs/{job_id}").json()
    assert job["state"] == "submitting"  # far past the attempts a real failure gets
    assert job["error"].startswith("waiting for spark: its name doesn't resolve")
    assert job["attempt"] == 1  # one attempt, same remote id: never re-run elsewhere
    host = client.get(f"/hosts/{down.host_id}").json()
    assert host["status"] == "error:unresolvable"
    down.down = None  # the network is back
    done = wait_job(client, job_id, TERMINAL, timeout=30)
    assert done["state"] == "succeeded" and done["error"] is None
    assert client.get(f"/hosts/{down.host_id}").json()["status"] == "online"


def test_the_wait_backs_off_and_a_host_back_online_retries_at_once(
    client: TestClient, down: DownableRunner
) -> None:
    ctx = ctx_of(client)
    job_id = add_job(client, down.host_id)
    wait_for(lambda: down.submits, lambda n: n >= 1, timeout=30)
    row = wait_for(
        lambda: ctx.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)),
        lambda j: j["next_attempt_at"] is not None,
        timeout=10,
    )
    assert 3.0 <= row["next_attempt_at"] - time.time() <= 5.5  # 5 s first
    time.sleep(1.0)
    assert down.submits == 1  # nothing in between: the host isn't hammered
    assert (scheduler_mod.HOST_WAIT_START, scheduler_mod.HOST_WAIT_MAX) == (5.0, 300.0)
    assert (hosts_mod.RECHECK_START, hosts_mod.RECHECK_MAX) == (60.0, 900.0)
    # A check finds the host back: its waiting job goes now, not in 5 s.
    down.down = None
    client.post(f"/hosts/{down.host_id}/check")
    assert wait_job(client, job_id, TERMINAL, timeout=4)["state"] == "succeeded"


def test_other_host_errors_still_fail_the_job(client: TestClient, down: DownableRunner) -> None:
    down.down = RunnerError("ssh: Permission denied (publickey).", transient=False,
                            code="auth_failed")  # fmt: skip
    job_id = add_job(client, down.host_id)
    done = wait_job(client, job_id, TERMINAL, timeout=30)
    assert done["state"] == "failed" and "Permission denied" in done["error"]


def test_a_running_job_is_never_finalized_while_its_host_is_down(
    client: TestClient, down: DownableRunner
) -> None:
    ctx = ctx_of(client)
    job_id = add_job(client, down.host_id)
    ctx.db.execute("UPDATE jobs SET state = 'running', attempt = 1, remote_id = 'r-1' "
                   "WHERE id = ?", (job_id,))  # fmt: skip
    wait_for(lambda: client.get(f"/jobs/{job_id}").json(),
             lambda j: (j["error"] or "").startswith("monitoring:"), timeout=30)  # fmt: skip
    time.sleep(0.5)
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"
    down.down = None
    assert wait_job(client, job_id, TERMINAL, timeout=30)["state"] == "succeeded"


# -- hosts come back by themselves ---------------------------------------------------------------


def test_a_host_in_error_is_rechecked_with_backoff_and_comes_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosts_mod, "RECHECK_START", 0.2)
    monkeypatch.setattr(hosts_mod, "RECHECK_MAX", 0.8)
    runner = DownableRunner(add_host(client, "spark", "error:unresolvable", {"gpus": []}))
    use_runner(client, runner)
    never = DownableRunner(add_host(client, "fresh", "error:unreachable", None))
    use_runner(client, never)
    keyed = DownableRunner(add_host(client, "keyed", "error:hostkey_changed", {"gpus": []}))
    use_runner(client, keyed)
    started = time.time()
    seen: list[float] = []

    def checks() -> int:
        if len(seen) < runner.checks:
            seen.append(time.time() - started)
        return runner.checks

    wait_for(checks, lambda n: n >= 4, timeout=30, interval=0.02)
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert seen[0] >= 0.15  # not at once: after RECHECK_START
    assert gaps[0] >= 0.3 and gaps[-1] <= 1.5, seen  # 0.4, 0.8, then capped at 0.8
    host = client.get(f"/hosts/{runner.host_id}").json()
    assert host["status"] == "error:unresolvable"
    runner.down = None  # back on the network
    wait_for(lambda: client.get(f"/hosts/{runner.host_id}").json()["status"], "online".__eq__,
             timeout=10)  # fmt: skip
    events = client.get(f"/events?entity_type=host&entity_id={runner.host_id}").json()
    assert "back_online" in [e["kind"] for e in events]
    # Never connected, or needing the user (a changed host key): never checked by itself.
    assert never.checks == 0 and keyed.checks == 0


# -- approved model services wait --------------------------------------------------------------


@pytest.fixture
def local(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    monkeypatch.setenv("NEWTON_SERVICE_STOP_GRACE", "2")
    # agentd checks This Mac once at start; its "back online" retries waiting work, so
    # let it finish before a test measures a backoff.
    wait_for(lambda: client.get("/hosts/local").json(), lambda h: h["last_checked_at"], timeout=60)
    yield client
    stop_services(client)


def as_ssh_host(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests script the local host's worker; only an SSH host can be "down", so
    let the manager judge failures as it would for one."""
    monkeypatch.setattr(manager.hosts, "is_down",
                        lambda host_id, error: hosts_mod.host_down(error, "ssh"))  # fmt: skip


def test_an_approved_service_waits_for_its_host(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "HOST_WAIT_START", 0.05)
    monkeypatch.setattr(manager_mod, "HOST_WAIT_MAX", 0.2)
    manager = ctx_of(local).services
    as_ssh_host(manager, monkeypatch)
    real = manager.worker
    starts = {"n": 0, "down": True}

    async def offline(host_id: str, method: str, path: str, **kw: Any) -> Any:
        if method == "POST" and path == "/services":
            starts["n"] += 1
            if starts["down"]:
                raise unresolvable()
        return await real(host_id, method, path, **kw)

    monkeypatch.setattr(manager, "worker", offline)
    svc = create(local)
    waiting = wait_for(lambda: local.get(f"/services/{svc['id']}").json(),
                       lambda s: starts["n"] >= 6, timeout=30)  # fmt: skip
    assert waiting["state"] == "approved"  # never failed for it
    assert waiting["error"].startswith("waiting for This Mac")
    starts["down"] = False
    ready = wait_state(local, svc["id"], "ready")
    assert ready["error"] is None


def test_a_service_wait_backs_off_from_5_seconds(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ctx_of(local).services
    as_ssh_host(manager, monkeypatch)
    real = manager.worker
    starts: list[float] = []

    async def offline(host_id: str, method: str, path: str, **kw: Any) -> Any:
        if method == "POST" and path == "/services":
            starts.append(time.time())
            raise RunnerError("worker unreachable: ConnectError()", code="unreachable")
        return await real(host_id, method, path, **kw)

    monkeypatch.setattr(manager, "worker", offline)
    svc = create(local)
    wait_for(lambda: starts, lambda s: len(s) >= 1, timeout=30)
    time.sleep(3.0)
    assert len(starts) == 1  # the next try is 5 s away, not the 2 s poll
    assert (manager_mod.HOST_WAIT_START, manager_mod.HOST_WAIT_MAX) == (5.0, 300.0)
    # The host coming back (any check or job seeing it) retries it now.
    manager._host_online("local")
    wait_for(lambda: starts, lambda s: len(s) >= 2, timeout=3)
    assert local.get(f"/services/{svc['id']}").json()["state"] == "approved"


# -- end to end over ssh: this Mac goes offline, then comes back ------------------------------


def flaky_ssh(tmp_path: Path, real_ssh: Path, down_file: Path) -> Path:
    """`ssh` that fails like OpenSSH on a Mac with no network while `down_file`
    exists (the -G config query and mux commands to a live master still work)."""
    script = tmp_path / "flaky_ssh.py"
    script.write_text(
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        f"if os.path.exists({str(down_file)!r}) and '-G' not in args and '-O' not in args:\n"
        f"    sys.stderr.write({RESOLVE[5:]!r} + '\\n')\n"
        "    sys.exit(255)\n"
        f"os.execv({str(real_ssh)!r}, [{str(real_ssh)!r}, *args])\n"
    )
    wrapper = tmp_path / "flaky-ssh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def test_offline_mac_job_waits_and_host_comes_back_by_itself(
    tmp_path: Path, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosts_mod, "RECHECK_START", 0.5)
    monkeypatch.setattr(hosts_mod, "RECHECK_MAX", 1.0)
    # The job's own retries are slow here: it goes on because the host came back.
    monkeypatch.setattr(scheduler_mod, "HOST_WAIT_START", 3600.0)
    offline = tmp_path / "offline"
    settings = make_settings(
        tmp_path,
        ssh_executable=str(flaky_ssh(tmp_path, fake_remote.ssh, offline)),
        ssh_keyscan_executable=str(fake_remote.keyscan),
    )
    with TestClient(create_app(settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False,
                                gpu_support="off")  # fmt: skip
        connected = client.post(f"/hosts/{host['id']}/connect")
        assert connected.status_code == 200, connected.text
        first = connected.json()["selftest_job_id"]
        assert wait_job(client, first, TERMINAL)["state"] == "succeeded"

        offline.touch()  # Wi-Fi off: the tunnel drops and the name no longer resolves
        drop_tunnel(client, host["id"])
        job = client.post(f"/hosts/{host['id']}/selftest", json={}).json()
        waiting = wait_for(lambda: client.get(f"/jobs/{job['id']}").json(),
                           lambda j: (j["error"] or "").startswith("waiting for spark:"),
                           timeout=60)  # fmt: skip
        assert waiting["state"] in ("queued", "submitting")
        assert "is this Mac offline?" in waiting["error"]
        wait_for(lambda: client.get(f"/hosts/{host['id']}").json()["status"],
                 "error:unresolvable".__eq__, timeout=30)  # fmt: skip
        time.sleep(1.5)  # a couple of automatic re-checks fail meanwhile
        assert client.get(f"/jobs/{job['id']}").json()["state"] in ("queued", "submitting")

        offline.unlink()  # back online: nobody presses Check
        wait_for(lambda: client.get(f"/hosts/{host['id']}").json()["status"],
                 "online".__eq__, timeout=60)  # fmt: skip
        done = wait_job(client, job["id"], TERMINAL, timeout=60)
        assert done["state"] == "succeeded", done


# -- ssh's own errors are not a host that is down ------------------------------------------


def test_an_ssh_config_error_fails_the_job_instead_of_waiting_forever(
    client: TestClient, down: DownableRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_mod, "BACKOFF_BASE", 0.02)
    monkeypatch.setattr(hosts_mod, "RECHECK_START", 0.05)
    down.down = RunnerError("ssh: /Users/me/.ssh/config: line 3: Bad configuration option: "
                            "foo", code="ssh_error")  # fmt: skip
    job_id = add_job(client, down.host_id)
    done = wait_job(client, job_id, TERMINAL, timeout=30)
    assert done["state"] == "failed", done
    assert done["error"].startswith("giving up after") and "Bad configuration" in done["error"]
    host = client.get(f"/hosts/{down.host_id}").json()
    assert host["status"] == "error:ssh_error"  # not trusted for placement meanwhile
    time.sleep(0.5)
    assert down.checks == 0  # and not re-checked by itself: it waits for the user


# -- a job cancelled before it started is stopped on its host, whatever happens meanwhile ----


class CancelScript(DownableRunner):
    """A worker host whose cancel fails as scripted (None: it works)."""

    def __init__(self, host_id: str, outcomes: list[RunnerError | None]) -> None:
        super().__init__(host_id)
        self.down = None
        self.outcomes = outcomes
        self.cancels: list[str] = []

    async def cancel(self, remote_id: str) -> dict[str, Any]:
        self.cancels.append(remote_id)
        self._gate()
        if self.outcomes and (outcome := self.outcomes.pop(0)) is not None:
            raise outcome
        return {"state": "cancelled"}


def cancelled_while_submitting(client: TestClient, host_id: str) -> str:
    """A job whose earlier submit may have reached its worker, cancelled meanwhile."""
    ctx = ctx_of(client)
    with ctx.db.tx():  # the scheduler never sees it queued
        job_id = create_job(ctx.db, ctx.settings, host_id=host_id, role="selftest",
                            label="selftest", manifest=selftest_manifest(),
                            bundle=build_bundle(None, {}))  # fmt: skip
        ctx.db.execute("UPDATE jobs SET state = 'submitting', attempt = 1, remote_id = 'r-1', "
                       "cancel_requested = 1, next_attempt_at = ? WHERE id = ?",
                       (time.time() + 3600, job_id))  # fmt: skip
    ctx.scheduler.wake()
    return job_id


def job_events(client: TestClient, job_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = client.get(f"/events?entity_type=job&entity_id={job_id}").json()
    return events


def test_an_unconfirmed_cancel_on_an_online_host_is_asked_again(client: TestClient) -> None:
    """The host stays online (it answered, just badly): it never "comes back", so the
    stop must not wait for that."""
    runner = CancelScript(add_host(client, "spark", "online", {"gpus": []}),
                          [RunnerError("worker answered 500", code="http_500")])  # fmt: skip
    use_runner(client, runner)
    job_id = cancelled_while_submitting(client, runner.host_id)
    job = wait_job(client, job_id, TERMINAL, timeout=30)
    assert job["state"] == "cancelled"
    assert job["error"].startswith(scheduler_mod.UNSTARTED_CANCEL_UNCONFIRMED)
    wait_for(lambda: runner.cancels, lambda c: len(c) >= 2, timeout=10)
    kinds = wait_for(lambda: [e["kind"] for e in job_events(client, job_id)],
                     lambda k: "remote_cancelled" in k, timeout=10)  # fmt: skip
    assert kinds.index("remote_cancel_pending") < kinds.index("remote_cancelled")
    assert runner.cancels == ["r-1", "r-1"]
    assert client.get(f"/hosts/{runner.host_id}").json()["status"] == "online"
    time.sleep(0.4)
    assert len(runner.cancels) == 2  # settled: never asked again
    assert job_id not in ctx_of(client).scheduler._orphans


def test_a_pending_remote_cancel_survives_a_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), headers=AUTH) as client:
        runner = CancelScript(add_host(client, "spark", "online", {"gpus": []}), [])
        runner.down = unresolvable()  # Wi-Fi off: the cancel can't reach the host
        use_runner(client, runner)
        host_id = runner.host_id
        job_id = cancelled_while_submitting(client, host_id)
        job = wait_job(client, job_id, TERMINAL, timeout=30)
        assert job["state"] == "cancelled"
        assert job["error"] == scheduler_mod.UNSTARTED_CANCEL_UNKNOWN
        assert client.get(f"/hosts/{host_id}").json()["status"] == "error:unresolvable"
    # agentd stopped (the shell closed) before the host was back; it starts again.
    with TestClient(create_app(settings), headers=AUTH) as client:
        scheduler = ctx_of(client).scheduler
        assert job_id in scheduler._orphans  # from its 'remote_cancel_pending' event
        again = CancelScript(host_id, [])
        use_runner(client, again)
        time.sleep(0.3)
        assert again.cancels == []  # the host is still down: not asked yet
        assert client.post(f"/hosts/{host_id}/check").status_code == 200  # it's back
        wait_for(lambda: again.cancels, lambda c: c == ["r-1"], timeout=10)
        wait_for(lambda: [e["kind"] for e in job_events(client, job_id)],
                 lambda k: "remote_cancelled" in k, timeout=10)  # fmt: skip
        assert job_id not in scheduler._orphans
        scheduler._load_orphans()  # settled: a later start doesn't ask again
        assert job_id not in scheduler._orphans
