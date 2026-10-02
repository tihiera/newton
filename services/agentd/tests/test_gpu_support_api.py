"""G4 through the API over the fake SSH: connecting a GB10-like box sets up GPU
support in the background with no extra step, a failing GPU is explained (and the
host keeps serving CPU work), "Install GPU support" retries on request, and agentd
itself re-runs GPU support when something changed (deferral, restart, driver)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, FakeRemote, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from pyenv_shims import (  # noqa: F401 - fixtures
    FAKE_CUPY,
    FAKE_DRIVER,
    base_python,
    make_fake_nvidia_smi,
    make_python_shim,
    wheelhouse,
)
from test_bootstrap_api import add_trusted_host

FLOAT64_SPEC = {
    "title": "t",
    "variants": [
        {"role": "baseline", "label": "b", "params": {"scheme": "upwind", "resolutions": [32, 64]}},
        {"role": "candidate", "label": "c",
         "params": {"scheme": "lax_wendroff", "resolutions": [32, 64]}},
    ],
}  # fmt: skip


@pytest.fixture
def gb10(
    fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch, wheelhouse: Path, tmp_path: Path
) -> FakeRemote:
    """The fake box with an NVIDIA driver, and a package index that has CuPy."""
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    monkeypatch.setenv("UV_FIND_LINKS", str(wheelhouse))
    bindir = tmp_path / "nvidia-bin"
    make_fake_nvidia_smi(bindir)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return fake_remote


def fake_gpu(remote: FakeRemote, mode: str) -> None:
    (remote.home / "fake-cupy").write_text(mode)


def dgx_python(base_python: Path, tmp_path: Path) -> Path:
    # Stock DGX OS: no python3-venv, PEP 668.
    return make_python_shim(
        tmp_path / "remote-bin" / "python3", base_python, no_ensurepip=True, pep668=True
    )


def gpu_settled(client: TestClient, host_id: str, timeout: float = 120) -> dict[str, Any]:
    """The host once no GPU support run is in progress."""
    host: dict[str, Any] = wait_for(
        lambda: client.get(f"/hosts/{host_id}").json(),
        lambda h: (h.get("gpu_task") or {}).get("state") != "running",
        timeout=timeout,
    )
    return host


def event_kinds(client: TestClient, host_id: str) -> list[str]:
    return [e["kind"] for e in client.get(f"/events?entity_id={host_id}").json()]


def test_connect_sets_up_cuda_without_asking(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path))
        assert host["gpu_support"] == "auto"
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 200, r.text
        assert r.json()["host"]["status"] == "online"  # CPU work doesn't wait for CuPy
        cuda = gpu_settled(client, host["id"])["capabilities"]["cuda"]
        assert cuda["ok"] is True, cuda
        assert (cuda["device"], cuda["cupy"], cuda["driver"]) == (
            "NVIDIA GB10", FAKE_CUPY, FAKE_DRIVER,
        )  # fmt: skip
        # The selftest queued by connect waited for the install, then ran.
        job = wait_for(
            lambda: client.get(f"/jobs/{r.json()['selftest_job_id']}").json(),
            lambda j: j["state"] in ("succeeded", "failed"),
            timeout=60,
        )
        assert job["state"] == "succeeded"
        # float64 work now goes to the Spark's GPU on its own.
        client.app.state.ctx.db.execute(  # type: ignore[attr-defined]
            "UPDATE hosts SET hardware = json_set(hardware, '$.packages.mlx', NULL) "
            "WHERE id = 'local'"
        )
        exp = client.post("/experiments", json=FLOAT64_SPEC).json()
        assert (exp["host_id"], exp["spec"]["backend"]) == (host["id"], "cuda")


def test_failing_gpu_is_explained_and_retried_on_request(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    fake_gpu(gb10, "sigbus")
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path))
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
        settled = gpu_settled(client, host["id"])
        assert settled["status"] == "online"  # CPU work is still fine
        cuda = settled["capabilities"]["cuda"]
        assert cuda["ok"] is False and "SIGBUS" in cuda["reason"]
        refused = client.post(
            "/experiments", json={**FLOAT64_SPEC, "host_id": host["id"], "backend": "cuda"}
        )
        assert refused.status_code == 400 and "SIGBUS" in refused.text

        r = client.post(f"/hosts/{host['id']}/gpu-support?wait=true")  # still broken: say why
        assert r.status_code == 502 and r.json()["code"] == "gpu_support_failed"
        assert "SIGBUS" in r.json()["error"]

        fake_gpu(gb10, "ok")  # e.g. after a reboot into the new driver
        started = client.post(f"/hosts/{host['id']}/gpu-support")  # the UI: don't block
        assert started.status_code == 202
        assert started.json()["gpu_task"]["state"] == "running"
        assert gpu_settled(client, host["id"])["capabilities"]["cuda"]["ok"] is True
        events = client.get(f"/events?entity_id={host['id']}").json()
        outcomes = [e["data"]["ok"] for e in events if e["kind"] == "gpu_support"]
        assert outcomes == [False, False, True]


def test_gpu_support_off_until_asked_for(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path), gpu_support="off")
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
        assert gpu_settled(client, host["id"])["gpu_task"] is None
        # The runner's own restart bootstrap (e.g. after a reboot) doesn't install it either.
        gb10.stop_worker()
        assert client.post(f"/hosts/{host['id']}/check").status_code == 200
        time.sleep(1.5)  # a few scheduler ticks
        assert not (gb10.fp / "gpu.json").exists()
        assert (
            "isn't installed" in gpu_settled(client, host["id"])["capabilities"]["cuda"]["reason"]
        )

        r = client.patch(f"/hosts/{host['id']}", json={"gpu_support": "auto"})
        assert r.status_code == 200 and r.json()["gpu_support"] == "auto"
        assert gpu_settled(client, host["id"])["capabilities"]["cuda"]["ok"] is True

        local = client.patch("/hosts/local", json={"gpu_support": "off"})
        assert local.status_code == 400
        assert client.patch(f"/hosts/{host['id']}", json={"gpu_support": "on"}).status_code == 422


def test_explicit_install_turns_an_off_host_on_only_when_it_works(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    fake_gpu(gb10, "wrong")
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path), gpu_support="off")
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 200
        wait_for(  # its selftest must finish first: packages never change under a job
            lambda: client.get(f"/jobs/{r.json()['selftest_job_id']}").json(),
            lambda j: j["state"] == "succeeded",
            timeout=60,
        )
        assert client.post(f"/hosts/{host['id']}/gpu-support?wait=true").status_code == 502
        assert client.get(f"/hosts/{host['id']}").json()["gpu_support"] == "off"
        fake_gpu(gb10, "ok")
        assert client.post(f"/hosts/{host['id']}/gpu-support?wait=true").status_code == 200
        assert client.get(f"/hosts/{host['id']}").json()["gpu_support"] == "auto"


def test_busy_host_says_so_and_stays_online(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path), gpu_support="off")
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
        job = client.post(f"/hosts/{host['id']}/selftest", json={"sleep": 20}).json()
        wait_for(
            lambda: client.get(f"/jobs/{job['id']}").json(),
            lambda j: j["state"] == "running",
            timeout=60,
        )
        r = client.post(f"/hosts/{host['id']}/gpu-support?wait=true")
        assert r.status_code == 409 and r.json()["code"] == "bootstrap_busy", r.text
        assert "jobs are running" in r.json()["error"]
        after = client.get(f"/hosts/{host['id']}").json()
        assert after["status"] == "online" and after["gpu_support"] == "off"
        client.post(f"/jobs/{job['id']}/cancel")


def test_deferred_setup_runs_once_the_jobs_finish(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path))
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
        assert gpu_settled(client, host["id"])["capabilities"]["cuda"]["ok"] is True
        # An upgrade bootstrap ran while a job did: the worker deferred GPU support.
        state = json.loads((gb10.fp / "gpu.json").read_text())
        state.update(ok=False, deferred=True, error="jobs were running; GPU support waits")
        (gb10.fp / "gpu.json").write_text(json.dumps(state))
        cuda = client.post(f"/hosts/{host['id']}/check").json()["capabilities"]["cuda"]
        assert "waits until the host's running jobs finish" in cuda["reason"]
        # Nobody presses anything: the scheduler notices the idle host and finishes it.
        settled = wait_for(
            lambda: client.get(f"/hosts/{host['id']}").json(),
            lambda h: h["capabilities"]["cuda"]["ok"],
            timeout=60,
        )
        assert settled["hardware"]["gpu_support"]["ok"] is True
        assert "gpu_support_due" in event_kinds(client, host["id"])


def test_no_job_starts_while_gpu_support_changes_packages(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    fake_gpu(gb10, "hang")  # a long smoke test keeps the run going
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path))
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 200
        assert client.get(f"/hosts/{host['id']}").json()["gpu_task"]["state"] == "running"
        time.sleep(2)
        assert client.get(f"/jobs/{r.json()['selftest_job_id']}").json()["state"] == "queued"


def test_worker_restart_refreshes_what_agentd_knows(
    ssh_settings: Settings, gb10: FakeRemote, base_python: Path, tmp_path: Path
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, dgx_python(base_python, tmp_path))
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
        assert gpu_settled(client, host["id"])["capabilities"]["cuda"]["ok"] is True
        # Reboot into a new driver: the worker is down, and CuPy must be re-verified.
        gb10.stop_worker()
        make_fake_nvidia_smi(Path(os.environ["PATH"].split(":")[0]), "580.105.08")
        job = client.post(f"/hosts/{host['id']}/selftest", json={}).json()  # uses the runner
        wait_for(
            lambda: client.get(f"/jobs/{job['id']}").json(),
            lambda j: j["state"] == "succeeded",
            timeout=90,
        )
        refreshed = wait_for(
            lambda: client.get(f"/hosts/{host['id']}").json(),
            lambda h: (
                (h["hardware"].get("gpu_support") or {}).get("driver") == "580.105.08"
                and h["capabilities"]["cuda"]["ok"]
            ),
            timeout=90,
        )
        assert refreshed["capabilities"]["cuda"]["driver"] == "580.105.08"
        assert "worker_restarted" in event_kinds(client, host["id"])
