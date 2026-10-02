"""SV2 review round: the rules of model services that must not regress (each one
was found broken or untested by the adversarial review)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import AUTH, FakeRemote, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.runners.base import RunnerError
from newton_agentd.serving.manager import port_free
from test_bootstrap_api import add_trusted_host
from test_services_api import FAKE, chat, create, stop_services, wait_reachable, wait_state


@pytest.fixture(autouse=True)
def small_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    monkeypatch.setenv("NEWTON_SERVICE_STOP_GRACE", "2")


@pytest.fixture
def local(client: TestClient) -> Iterator[TestClient]:
    yield client
    stop_services(client)


def manager_of(client: TestClient) -> Any:
    return client.app.state.ctx.services  # type: ignore[attr-defined]


def wrap_worker(client: TestClient, monkeypatch: pytest.MonkeyPatch, hook: Any) -> None:
    manager = manager_of(client)
    real = manager.worker

    async def wrapped(host_id: str, method: str, path: str, **kw: Any) -> Any:
        return await hook(real, host_id, method, path, **kw)

    monkeypatch.setattr(manager, "worker", wrapped)


def worker_status(client: TestClient, sid: str) -> dict[str, Any]:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    path = Path(ctx.settings.local_worker_root) / "services" / sid / "status.json"
    out: dict[str, Any] = json.loads(path.read_text())
    return out


# -- the start/stop race and the API key ---------------------------------------------------


def test_cancel_while_the_start_is_in_flight_leaves_nothing_running(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_start(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        out = await real(host_id, method, path, **kw)
        if method == "POST" and path == "/services":
            await asyncio.sleep(2)  # the worker started it; the answer is slow
        return out

    wrap_worker(local, monkeypatch, slow_start)
    svc = create(local)
    time.sleep(0.7)  # the loop is inside the slow POST
    assert local.post(f"/services/{svc['id']}/stop").json()["state"] == "cancelled"
    wait_for(lambda: worker_status(local, svc["id"])["state"],
             lambda s: s in ("stopped", "stopping"), timeout=30)  # fmt: skip
    wait_for(lambda: worker_status(local, svc["id"])["state"], lambda s: s == "stopped",
             timeout=30)  # fmt: skip
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    assert ctx.secrets.get(f"service-key-{svc['id']}") is None  # no key left behind


def test_a_retried_start_keeps_the_key(local: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker created it, but the answer was lost: the start is sent again, and
    # the worker answers "already exists" (no key in that answer).
    async def lost_answer(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        out = await real(host_id, method, path, **kw)
        if method == "POST" and path == "/services":
            return await real(host_id, method, path, **kw)  # the retry's answer
        return out

    wrap_worker(local, monkeypatch, lost_answer)
    svc = create(local)
    wait_state(local, svc["id"], "ready")
    creds = local.get(f"/services/{svc['id']}/credentials").json()
    assert creds["api_key"] and chat(creds, "still keyed").status_code == 200


def test_the_key_never_reaches_the_database(local: TestClient) -> None:
    svc = create(local)
    wait_state(local, svc["id"], "ready")
    creds = local.get(f"/services/{svc['id']}/credentials").json()
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    row = ctx.db.query_one("SELECT api_key_ref FROM services WHERE id = ?", (svc["id"],))
    assert row["api_key_ref"] == f"service-key-{svc['id']}"
    db_bytes = b"".join(p.read_bytes() for p in Path(ctx.settings.data_dir).glob("newton.db*"))
    assert creds["api_key"].encode() not in db_bytes
    local.post(f"/services/{svc['id']}/stop")
    wait_state(local, svc["id"], "stopped")
    assert ctx.secrets.get(f"service-key-{svc['id']}") is None  # removed once it ended


# -- refusals and approvals -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("admission", "message"),
    [
        ({"engine_installed": False}, "engine is not installed"),
        ({"disk": False, "disk_free_gb": 3.0}, "not enough disk"),
        ({"sizing": "120 GB is more than vLLM may take on this host"}, "more than vLLM"),
    ],
)
def test_refused_before_anyone_is_asked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, admission: dict[str, Any], message: str
) -> None:
    async def verdict(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        out = await real(host_id, method, path, **kw)
        return {**out, **admission} if path == "/services/admission" else out

    wrap_worker(client, monkeypatch, verdict)
    r = client.post("/services", json={"host_id": "local", "name": "x", "settings": FAKE})
    assert r.status_code == 409 and message in r.json()["error"], r.text
    assert client.get("/services").json() == [] and client.get("/approvals").json() == []


def test_worker_rejections_map_to_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        raise RunnerError("worker POST /services/admission → 400: bad", code="http_400",
                          transient=False)  # fmt: skip

    wrap_worker(client, monkeypatch, reject)
    r = client.post("/services", json={"host_id": "local", "name": "x", "settings": FAKE})
    assert r.status_code == 409 and client.get("/services").json() == []


def test_model_supplied_code_needs_approval(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def vllm_ok(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        if path == "/services/admission":  # vLLM isn't installed here: pretend it is
            return {"ok": True, "need_gb": 8, "available_gb": 100, "pending_gb": 0,
                    "reserve_gb": 8, "model_present": True, "engine_installed": True}  # fmt: skip
        return await real(host_id, method, path, **kw)

    wrap_worker(client, monkeypatch, vllm_ok)
    r = client.post("/services", json={"host_id": "local", "name": "code", "settings": {
        "engine": "vllm", "model": "org/model", "revision": "c" * 40, "memory_gb": 8,
        "trust_remote_code": True}})  # fmt: skip
    assert r.status_code == 201 and r.json()["state"] == "awaiting_approval"
    approval = client.get("/approvals?status=pending").json()[0]
    assert "trust_remote_code" in approval["title"] and approval["details"]["download"] is False
    client.post(f"/approvals/{approval['id']}/reject")
    rejected = client.get(f"/services/{r.json()['id']}").json()
    assert rejected["state"] == "rejected" and rejected["finished_at"]


# -- the worker's own outcomes ------------------------------------------------------------------


def test_a_crashed_engine_is_failed_and_no_longer_served(local: TestClient) -> None:
    svc = create(local, fake={"crash_after_s": 2})
    failed = wait_state(local, svc["id"], "failed")
    assert "exit code 3" in failed["error"] and failed["endpoint"] is None
    assert local.get(f"/services/{svc['id']}/credentials").status_code == 400


def test_a_service_the_worker_forgot_is_lost(local: TestClient) -> None:
    svc = create(local)
    ready = wait_state(local, svc["id"], "ready")
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    local.post(f"/services/{svc['id']}/stop")
    wait_state(local, svc["id"], "stopped")
    # A service the worker has no record of at all (its root wiped, a new install):
    ctx.db.execute("UPDATE services SET state = 'ready', finished_at = NULL WHERE id = ?",
                   (svc["id"],))  # fmt: skip
    import shutil

    shutil.rmtree(Path(ctx.settings.local_worker_root) / "services" / svc["id"])
    lost = wait_state(local, svc["id"], "lost")
    assert "no record" in lost["error"] and ready["remote_port"]


def test_logs_of_a_service_that_never_started(local: TestClient,
                                              monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    async def refuse_start(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        if method == "POST" and path == "/services":
            raise RunnerError("worker POST /services → 409: not enough memory",
                              code="http_409", transient=False)  # fmt: skip
        return await real(host_id, method, path, **kw)

    wrap_worker(local, monkeypatch, refuse_start)
    svc = create(local)
    wait_state(local, svc["id"], "failed")
    assert local.get(f"/services/{svc['id']}/logs").json()["data"] == ""
    assert local.get(f"/services/{svc['id']}/logs?offset=-1").status_code == 422


def test_one_slow_host_does_not_hold_up_the_others(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = create(local, name="slow")
    second = create(local, name="fast")
    wait_state(local, first["id"], "ready")
    wait_state(local, second["id"], "ready")
    polls: list[float] = []

    async def stall_one(real: Any, host_id: str, method: str, path: str, **kw: Any) -> Any:
        if path == f"/services/{first['id']}":
            await asyncio.sleep(15)
        if path == f"/services/{second['id']}":
            polls.append(time.time())
        return await real(host_id, method, path, **kw)

    wrap_worker(local, monkeypatch, stall_one)
    time.sleep(8)
    # Waiting on the stalled one would allow at most one poll of the other in 8 s.
    assert len(polls) >= 2, polls


def answers(creds: dict[str, Any]) -> bool:
    try:
        return chat(creds, "back").status_code == 200
    except httpx.HTTPError:
        return False


# -- the forward on SSH hosts ---------------------------------------------------------------------


def test_a_broken_forward_with_a_live_master_is_rebuilt(
    ssh_settings: Settings, fake_remote: FakeRemote, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    drop = tmp_path / "drop-forwards"
    monkeypatch.setenv("FAKESSH_FORWARD_DROP_FILE", str(drop))
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        try:
            host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False,
                                    gpu_support="off")  # fmt: skip
            assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
            svc = create(client, host["id"])
            ready = wait_reachable(client, svc["id"])
            manager = manager_of(client)
            first = manager._tunnels[svc["id"]]
            creds = client.get(f"/services/{svc['id']}/credentials").json()
            # The view's endpoint and the credentials are the forward's end on this Mac.
            assert creds["base_url"] == ready["endpoint"]["base_url"]
            assert creds["base_url"].endswith(f":{first.local_port}/v1")
            drop.touch()  # the master stays alive; its forward stops working
            manager._last_probe.clear()
            wait_for(lambda: client.get(f"/services/{svc['id']}").json()["endpoint"],
                     lambda e: e and e["reachable"] is False, timeout=60)  # fmt: skip
            wait_for(lambda: manager._tunnels.get(svc["id"]) is not first, bool, timeout=60)
            drop.unlink()
            wait_for(lambda: answers(creds), bool, timeout=60)
            client.post(f"/services/{svc['id']}/stop")
            wait_state(client, svc["id"], "stopped")
            wait_for(lambda: svc["id"] not in manager._tunnels, bool, timeout=30)
            last = manager._tunnels.get(svc["id"])
            assert last is None and port_free(first.local_port)  # nothing holds the port
        finally:
            stop_services(client)


def test_force_deleting_a_host_cancels_its_waiting_services(
    ssh_settings: Settings, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        try:
            host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False,
                                    gpu_support="off")  # fmt: skip
            assert client.post(f"/hosts/{host['id']}/connect").status_code == 200

            async def download(real: Any, host_id: str, method: str, path: str,
                               **kw: Any) -> Any:  # fmt: skip
                out = await real(host_id, method, path, **kw)
                return {**out, "model_present": False} if path == "/services/admission" else out

            wrap_worker(client, monkeypatch, download)
            waiting = create(client, host["id"], name="waits")
            assert waiting["state"] == "awaiting_approval"
            assert client.delete(f"/hosts/{host['id']}?force=true").status_code == 204
            gone = client.get(f"/services/{waiting['id']}").json()
            assert gone["state"] == "cancelled" and gone["error"] == "its host was deleted"
            assert client.get("/approvals?status=pending").json() == []
            ctx = client.app.state.ctx  # type: ignore[attr-defined]
            assert ctx.secrets.get(f"service-key-{waiting['id']}") is None
        finally:
            stop_services(client)


def test_probes_never_use_a_proxy(local: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # would black-hole everything
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    svc = create(local)
    reachable = wait_reachable(local, svc["id"])
    assert reachable["endpoint"]["reachable"] is True  # worker, probe: all direct
