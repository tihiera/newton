"""SV2: model services managed by agentd, end to end with the fake engine.

Local host (no forward) and a "remote" host through the fake SSH (a private TCP
forward to this Mac). Approval when a download is needed, refusal up front when
the host can't take it, API keys in the secret store, stop/drain, the reconcile
loop picking everything up after an agentd restart, and host deletion.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import AUTH, FakeRemote, make_settings, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.secrets import MemorySecretStore
from test_bootstrap_api import add_trusted_host

FAKE = {"engine": "fake", "model": "fake/echo", "memory_gb": 0.5, "startup_timeout_s": 60}


@pytest.fixture(autouse=True)
def small_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    # This Mac's free memory varies; the admission rules have their own worker tests.
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    monkeypatch.setenv("NEWTON_SERVICE_STOP_GRACE", "2")


def stop_services(client: TestClient) -> None:
    """Never leave a model server running (their supervisors outlive the test)."""
    for s in client.get("/services").json():
        if s["state"] in ("starting", "ready", "draining"):
            client.post(f"/services/{s['id']}/stop")
    for s in client.get("/services").json():
        if s["state"] in ("starting", "ready", "draining", "stopping"):
            wait_state(client, s["id"], "stopped", "failed", "lost", timeout=30)


@pytest.fixture
def stop_all() -> Iterator[list[TestClient]]:
    """For the shared `client`: stopped before the app closes."""
    clients: list[TestClient] = []
    yield clients
    for c in clients:
        stop_services(c)


def create(client: TestClient, host_id: str = "local", name: str = "echo",
           **settings: Any) -> dict[str, Any]:  # fmt: skip
    r = client.post("/services", json={"host_id": host_id, "name": name,
                                       "settings": {**FAKE, **settings}})  # fmt: skip
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def wait_state(client: TestClient, sid: str, *states: str, timeout: float = 60) -> dict[str, Any]:
    out: dict[str, Any] = wait_for(
        lambda: client.get(f"/services/{sid}").json(),
        lambda s: s["state"] in states,
        timeout=timeout,
    )
    return out


def wait_reachable(client: TestClient, sid: str) -> dict[str, Any]:
    out: dict[str, Any] = wait_for(
        lambda: client.get(f"/services/{sid}").json(),
        lambda s: (s.get("endpoint") or {}).get("reachable") is True,
        timeout=60,
    )
    return out


def chat(creds: dict[str, Any], text: str, key: str | None = "use") -> httpx.Response:
    api_key = creds["api_key"] if key == "use" else key
    return httpx.post(
        f"{creds['base_url']}/chat/completions",
        json={"model": creds["model"], "messages": [{"role": "user", "content": text}]},
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        timeout=10,
    )


def test_local_service_lifecycle(client: TestClient, stop_all: list[TestClient]) -> None:
    stop_all.append(client)
    svc = create(client)
    assert svc["state"] == "approved"  # nothing to download: no approval needed
    ready = wait_state(client, svc["id"], "ready")
    assert ready["endpoint"]["auth"] == "bearer" and ready["healthy"] is True
    assert "api_key_ref" not in ready  # never shown
    creds = client.get(f"/services/{svc['id']}/credentials").json()
    assert creds["base_url"] == ready["endpoint"]["base_url"] and len(creds["api_key"]) >= 32
    assert chat(creds, "hello").json()["choices"][0]["message"]["content"] == "echo: hello"
    assert chat(creds, "no key", key=None).status_code == 401
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    row = ctx.db.query_one("SELECT api_key_ref FROM services WHERE id = ?", (svc["id"],))
    assert ctx.secrets.get(row["api_key_ref"]) == creds["api_key"]  # key in the secret store
    logs = client.get(f"/services/{svc['id']}/logs").json()
    assert "fake model server" in logs["data"]

    stopping = client.post(f"/services/{svc['id']}/stop").json()
    assert stopping["state"] in ("stopping", "stopped")
    stopped = wait_state(client, svc["id"], "stopped")
    assert stopped["endpoint"] is None
    assert client.get(f"/services/{svc['id']}/credentials").status_code == 400
    kinds = [e["kind"] for e in client.get(f"/events?entity_id={svc['id']}").json()]
    assert "created" in kinds and "started_on_worker" in kinds


def test_remote_service_is_reached_through_a_private_forward(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        try:
            _remote_service(client)
        finally:
            stop_services(client)


def _remote_service(client: TestClient) -> None:
    host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False,
                            gpu_support="off")  # fmt: skip
    assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
    svc = create(client, host["id"])
    ready = wait_reachable(client, svc["id"])
    local = int(ready["endpoint"]["base_url"].split(":")[2].split("/")[0])
    assert local != ready["remote_port"]  # this Mac's end of the forward
    creds = client.get(f"/services/{svc['id']}/credentials").json()
    reply = chat(creds, "over ssh").json()
    assert reply["choices"][0]["message"]["content"] == "echo: over ssh"

    # The forward dies (e.g. sleep): the loop notices and builds a new one.
    manager = client.app.state.ctx.services  # type: ignore[attr-defined]
    tunnel = manager._tunnels[svc["id"]]
    os.kill(tunnel.proc.pid, signal.SIGKILL)
    manager._last_probe.clear()

    def answers() -> bool:
        try:
            return chat(creds, "again").status_code == 200
        except httpx.HTTPError:
            return False

    wait_for(answers, bool, timeout=60)  # the same base_url works again
    forwards = wait_for(lambda: [e for e in client.get(f"/events?entity_id={svc['id']}").json()
                                 if e["kind"] == "forwarded"],
                        lambda f: len(f) >= 2, timeout=30)  # fmt: skip
    assert forwards[-1]["data"]["local_port"] == local  # same port

    client.post(f"/services/{svc['id']}/stop")
    wait_state(client, svc["id"], "stopped")
    wait_for(lambda: svc["id"] in manager._tunnels, lambda present: not present)


def test_downloads_need_approval(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                 stop_all: list[TestClient]) -> None:  # fmt: skip
    stop_all.append(client)
    manager = client.app.state.ctx.services  # type: ignore[attr-defined]
    real = manager.worker

    async def model_missing(host_id: str, method: str, path: str, **kw: Any) -> Any:
        out = await real(host_id, method, path, **kw)
        if path == "/services/admission":
            out = {**out, "model_present": False}  # as if it had to be downloaded
        return out

    monkeypatch.setattr(manager, "worker", model_missing)
    pending = create(client, name="needs-ok")
    assert pending["state"] == "awaiting_approval"
    approval = client.get("/approvals?status=pending").json()[0]
    assert approval["details"]["download"] is True and "downloads fake/echo" in approval["title"]
    client.post(f"/approvals/{approval['id']}/approve")
    wait_state(client, pending["id"], "ready")

    refused = create(client, name="refused")
    approval = client.get("/approvals?status=pending").json()[0]
    client.post(f"/approvals/{approval['id']}/reject")
    assert client.get(f"/services/{refused['id']}").json()["state"] == "rejected"

    cancelled = create(client, name="changed-my-mind")
    assert client.post(f"/services/{cancelled['id']}/stop").json()["state"] == "cancelled"
    assert client.get("/approvals?status=pending").json() == []  # its approval went too


def test_a_host_that_cannot_take_it_refuses_up_front(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set before agentd starts: it starts this Mac's worker at once (to check it), and
    # the worker reads the reserve from its environment.
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "100000")  # nothing fits
    with TestClient(create_app(settings), headers=AUTH) as client:
        refuses_up_front(client)


def refuses_up_front(client: TestClient) -> None:
    r = client.post("/services", json={"host_id": "local", "name": "x", "settings": FAKE})
    assert r.status_code == 409 and "not enough memory" in r.json()["error"]
    assert client.get("/services").json() == []  # nothing was created


def test_specs_are_checked_before_anything_happens(client: TestClient) -> None:
    for settings in (
        {**FAKE, "engine": "ollama", "model": "llama3.2"},  # no pinned digest
        {**FAKE, "engine": "vllm", "model": "--trust-remote-code/x", "revision": "c" * 40},
        {**FAKE, "args": "--anything"},
    ):
        r = client.post("/services", json={"host_id": "local", "name": "x",
                                           "settings": settings})  # fmt: skip
        assert r.status_code == 422, settings
    r = client.post("/services", json={"host_id": "nope", "name": "x", "settings": FAKE})
    assert r.status_code == 404


def test_drain(client: TestClient, stop_all: list[TestClient]) -> None:
    stop_all.append(client)
    svc = create(client)
    wait_state(client, svc["id"], "ready")
    draining = client.post(f"/services/{svc['id']}/drain?seconds=1").json()
    assert draining["state"] == "draining" and draining["endpoint"] is not None
    assert wait_state(client, svc["id"], "stopped")["endpoint"] is None


def test_agentd_restart_picks_services_up_again(tmp_path: Path, stop_all: list[TestClient]) -> None:
    store = MemorySecretStore()  # the Keychain outlives agentd; so does this store
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, secret_store=store), headers=AUTH) as first:
        svc = create(first)
        ready = wait_state(first, svc["id"], "ready")
    with TestClient(create_app(make_settings(tmp_path), secret_store=store),
                    headers=AUTH) as second:  # fmt: skip
        try:
            again = wait_reachable(second, svc["id"])
            assert again["state"] == "ready" and again["remote_port"] == ready["remote_port"]
            creds = second.get(f"/services/{svc['id']}/credentials").json()
            assert chat(creds, "still here").status_code == 200
        finally:
            stop_services(second)


def test_deleting_a_host_with_services(ssh_settings: Settings, fake_remote: FakeRemote) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False,
                                gpu_support="off")  # fmt: skip
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 200
        svc = create(client, host["id"])
        ready = wait_state(client, svc["id"], "ready")
        refused = client.delete(f"/hosts/{host['id']}")
        assert refused.status_code == 400 and "model service" in refused.json()["error"]
        assert client.delete(f"/hosts/{host['id']}?force=true").status_code == 204
        assert wait_state(client, svc["id"], "lost")["error"] == "its host was deleted"
        status = (fake_remote.fp / "services" / svc["id"] / "status.json").read_text()
        assert '"stopping"' in status or '"stopped"' in status  # asked the worker to stop
        assert ready["remote_port"]
