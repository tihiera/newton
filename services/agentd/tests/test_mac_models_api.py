"""SV4 through agentd: Mac models are off until the profile turns them on, run on this
Mac only, and stop (out of the router) when the Mac goes on battery."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, make_settings, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from test_services_api import FAKE, stop_services, wait_state

REV = "a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3"
MLX = {"engine": "mlx", "model": "mlx-community/Qwen2.5-0.5B-Instruct-4bit",
       "revision": REV, "memory_gb": 1, "parallel": 2}  # fmt: skip


@pytest.fixture
def gate_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # This Mac's real power state must not decide the tests: fake pmset/sysctl first on
    # PATH (on AC, no pressure), inherited by the local worker started below.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, out in (("pmset", "Now drawing from 'AC Power'"), ("sysctl", "1")):
        script = bin_dir / name
        script.write_text(f'#!/bin/sh\necho "{out}"\n')
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    path = tmp_path / "mac-gate"
    monkeypatch.setenv("NEWTON_MAC_GATE_FILE", str(path))  # before the local worker starts
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    return path


@pytest.fixture
def mac(tmp_path: Path, gate_file: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path)), headers=AUTH) as client:
        try:
            yield client
        finally:
            stop_services(client)


def create(client: TestClient, settings: dict[str, Any]) -> Any:
    return client.post("/services", json={"host_id": "local", "name": "mac", "settings": settings})


def test_mac_models_are_off_until_the_profile_says_so(mac: TestClient) -> None:
    assert mac.get("/profile").json()["mac_models"] is False
    refused = create(mac, MLX)
    assert refused.status_code == 409 and "mac_models" in refused.json()["error"]
    assert mac.get("/services").json() == []
    assert mac.patch("/profile", json={"mac_models": True}).json()["mac_models"] is True
    assert mac.patch("/profile", json={"display_name": "me"}).json()["mac_models"] is True
    big = create(mac, {**MLX, "memory_gb": 32})
    assert big.status_code == 422  # small models only
    eight_bit = create(mac, {**MLX, "model": "mlx-community/Qwen2.5-7B-Instruct-8bit"})
    assert eight_bit.status_code == 422


def test_on_battery_nothing_starts_and_what_runs_stops(mac: TestClient, gate_file: Path) -> None:
    mac.patch("/profile", json={"mac_models": True})
    gate_file.write_text("battery")
    refused = create(mac, {**FAKE, "fake": {"mac_gated": True}})
    assert refused.status_code == 409 and "battery" in refused.json()["error"]
    gate_file.write_text("")
    svc = create(mac, {**FAKE, "fake": {"mac_gated": True}}).json()
    wait_for(lambda: mac.get(f"/services/{svc['id']}").json(),
             lambda s: (s.get("endpoint") or {}).get("reachable"), timeout=60)  # fmt: skip
    ask = {"model": "fake/echo", "messages": [{"role": "user", "content": "hi"}]}
    assert mac.post("/v1/chat/completions", json=ask).status_code == 200
    gate_file.write_text("pressure")
    stopped = wait_state(mac, svc["id"], "stopped", timeout=30)
    assert "memory pressure" in stopped["error"]
    gone = mac.post("/v1/chat/completions", json=ask)
    assert gone.status_code == 404  # out of the router


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("NEWTON_TEST_MAC_MODELS"),
                    reason="NEWTON_TEST_MAC_MODELS not set (downloads ~290 MB)")  # fmt: skip
def test_live_mlx_model_through_the_router(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path)), headers=AUTH) as client:
        try:
            client.patch("/profile", json={"mac_models": True})
            r = create(client, MLX)
            assert r.status_code == 201, r.text
            approval = client.get("/approvals?status=pending").json()[0]
            print("approval:", approval["title"])
            client.post(f"/approvals/{approval['id']}/approve")
            t0 = time.time()
            up = wait_for(lambda: client.get(f"/services/{r.json()['id']}").json(),
                          lambda s: s["state"] in ("failed", "stopped")
                          or (s.get("endpoint") or {}).get("reachable"),
                          timeout=900, interval=2)  # fmt: skip
            assert up["state"] == "ready", up["error"]
            print(f"ready in {time.time() - t0:.1f} s (download + load)")
            key = client.get("/router/credentials").json()["api_key"]
            t1 = time.time()
            question = {
                "model": MLX["model"],
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "In one word: is 2+2=4?"}],
            }
            answer = client.post("/v1/chat/completions", json=question,
                                 headers={"Authorization": f"Bearer {key}"})  # fmt: skip
            print(f"answer in {time.time() - t1:.2f} s:", answer.json())
            assert answer.status_code == 200 and answer.headers["X-Newton-Engine"] == "mlx"
            assert answer.headers["X-Newton-Revision"] == REV
            assert answer.json()["choices"][0]["message"]["content"]
        finally:
            stop_services(client)


def test_an_mlx_engine_only_ever_gets_standard_fields(mac: TestClient) -> None:
    """mlx-lm loads whatever path `model`, `draft_model` or `adapters` names: the router
    sends it standard OpenAI fields only, and `model` is always its own snapshot."""
    import httpx

    svc = create(mac, FAKE).json()
    ready = wait_for(lambda: mac.get(f"/services/{svc['id']}").json(),
                     lambda s: (s.get("endpoint") or {}).get("reachable"), timeout=60)  # fmt: skip
    ctx = mac.app.state.ctx  # type: ignore[attr-defined]
    real = ctx.router.endpoints

    def as_mlx() -> list[Any]:
        out = real()
        for e in out:
            e.engine, e.context_length = "mlx", 256
        return out

    ctx.router.endpoints = as_mlx
    r = mac.post("/v1/chat/completions", json={
        "model": "fake/echo", "messages": [{"role": "user", "content": "x"}],
        "draft_model": "/etc", "adapters": "/tmp/x", "num_draft_tokens": 9,
        "max_tokens": 100000})  # fmt: skip
    assert r.status_code == 200, r.text
    seen = httpx.get(f"http://127.0.0.1:{ready['remote_port']}/stats", trust_env=False).json()
    assert seen["last_body"] == {"model": "default_model"}
    assert "draft_model" not in seen["last_keys"] and "adapters" not in seen["last_keys"]
    assert "num_draft_tokens" not in seen["last_keys"]


def test_turning_mac_models_off_stops_them(mac: TestClient) -> None:
    mac.patch("/profile", json={"mac_models": True})
    svc = create(mac, FAKE).json()
    wait_state(mac, svc["id"], "ready")
    ctx = mac.app.state.ctx  # type: ignore[attr-defined]
    spec = json.loads(ctx.db.query_one("SELECT spec FROM services WHERE id = ?",
                                       (svc["id"],))["spec"])  # fmt: skip
    ctx.db.execute(
        "UPDATE services SET spec = ? WHERE id = ?",
        (json.dumps({**spec, "engine": "mlx"}), svc["id"]),
    )  # as if it were one
    mac.patch("/profile", json={"mac_models": False})
    assert wait_state(mac, svc["id"], "stopped", timeout=30)["state"] == "stopped"


def test_with_mac_models_on_an_mlx_service_waits_for_approval(mac: TestClient) -> None:
    mac.patch("/profile", json={"mac_models": True})
    r = create(mac, MLX)
    assert r.status_code == 201, r.text  # the engine is here (this venv has mlx-lm)
    assert r.json()["state"] == "awaiting_approval"  # a download: asked first
    approval = mac.get("/approvals?status=pending").json()[0]
    assert "mlx-community/Qwen2.5-0.5B-Instruct-4bit" in approval["title"]
    assert mac.post(f"/services/{r.json()['id']}/stop").json()["state"] == "cancelled"
    assert create(mac, {**MLX, "revision": None}).status_code == 422  # pinned only
    mac.patch("/profile", json={"mac_models": False})
    assert create(mac, MLX).status_code == 409


def test_mlx_models_run_on_this_mac_only(ssh_settings: Any, fake_remote: Any) -> None:
    import sys

    from test_bootstrap_api import add_trusted_host

    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        client.patch("/profile", json={"mac_models": True})
        host = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False,
                                gpu_support="off")  # fmt: skip
        r = client.post("/services", json={"host_id": host["id"], "name": "m", "settings": MLX})
        assert r.status_code == 409 and "this Mac only" in r.json()["error"]
