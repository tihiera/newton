"""B7, a clean Mac: built-in schemes compared with no paper, and GET /readiness (what
this Mac can do now, from cached state only). Nothing here reaches the network."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import AUTH, make_settings
from fastapi.testclient import TestClient
from newton_agentd import runtime_check
from newton_agentd.app import create_app
from newton_agentd.research import schemes

ROOT = Path(__file__).resolve().parents[3]
IR = schemes.ir_module(ROOT / "benchmarks")
KEYS = ["engine", "cpu", "metal", "reader", "gpu_host", "keychain"]


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any HTTP from agentd beyond this Mac (the local worker answers on loopback) fails
    as if the Mac were offline, and is recorded."""
    calls: list[str] = []
    send = httpx.AsyncClient.send

    async def no_network(self: httpx.AsyncClient, request: httpx.Request,
                         **kwargs: Any) -> httpx.Response:  # fmt: skip
        if request.url.host in ("127.0.0.1", "localhost"):
            return await send(self, request, **kwargs)
        calls.append(str(request.url))
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    return calls


@pytest.fixture
def client(tmp_path: Path, offline: list[str]) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path)), headers=AUTH) as c:
        yield c


def ctx_of(client: TestClient) -> Any:
    return client.app.state.ctx  # type: ignore[attr-defined]


# -- built-in schemes, no paper ---------------------------------------------------------


def test_library_experiment_awaits_approval_with_the_library_documents(
    client: TestClient, offline: list[str]
) -> None:
    r = client.post("/experiments/library", json={
        "candidates": ["muscl_koren", "muscl_vanleer_ssprk2", "muscl_koren"],
        "host_id": "local", "backend": "cpu",
    })  # fmt: skip
    assert r.status_code == 201, r.text
    exp = r.json()
    assert exp["state"] == "awaiting_approval"
    assert exp["research_item_id"] is None
    variants = exp["spec"]["variants"]
    assert [(v["role"], v["label"]) for v in variants] == [
        ("baseline", "upwind"), ("candidate", "muscl_koren"),
        ("candidate", "muscl_vanleer_ssprk2"),
    ]  # fmt: skip
    assert variants[0]["params"]["scheme"] == "upwind"  # hand-written: runs as itself
    library = {s["name"]: s for s in client.get("/schemes").json()}
    for v in variants[1:]:
        assert v["params"]["scheme"] == "ir"
        assert IR.digest(v["params"]["scheme_ir"]) == library[v["label"]]["digest"]
    # One problem for all: the step the most restrictive scheme claims (SSP-RK2: 0.5).
    assert {v["params"]["cfl"] for v in variants} == {0.5}
    assert {v["params"]["initial_condition"] for v in variants} == {"sine"}
    pending = client.get("/approvals?status=pending").json()
    assert [a["subject_id"] for a in pending] == [exp["id"]]
    assert offline == []


def test_library_baseline_from_the_library_and_a_goal(client: TestClient) -> None:
    goal = client.post("/goals", json={"title": "limiters"}).json()
    r = client.post("/experiments/library", json={
        "goal_id": goal["id"], "candidates": ["muscl_mc"], "baseline": "muscl_superbee",
        "initial_condition": "square", "host_id": "local", "backend": "cpu",
    })  # fmt: skip
    assert r.status_code == 201, r.text
    exp = r.json()
    assert exp["goal_id"] == goal["id"]
    baseline = exp["spec"]["variants"][0]
    assert baseline["params"]["scheme"] == "ir"  # not hand-written: as its document
    assert baseline["params"]["scheme_ir"]["name"] == "muscl_superbee"
    assert baseline["params"]["cfl"] == 0.8


def test_library_refuses_unknown_names(client: TestClient) -> None:
    def post(**body: Any) -> httpx.Response:
        return client.post("/experiments/library",
                           json={"host_id": "local", "backend": "cpu", **body})  # fmt: skip

    r = post(candidates=["muscl_koren", "rm -rf"])
    assert r.status_code == 400 and "'rm -rf'" in r.json()["error"]
    assert "muscl_koren'" not in r.json()["error"].split("(")[0]  # only the unknown one
    assert post(candidates=["muscl_mc"], baseline="nope").status_code == 400
    assert post(candidates=["upwind"]).status_code == 400  # the baseline itself
    assert post(candidates=[]).status_code == 422
    assert post(candidates=["muscl_mc"], goal_id="goal_missing").status_code == 400
    assert client.get("/experiments").json() == []


# -- readiness ---------------------------------------------------------------------------


def readiness(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/readiness")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["key"] for i in items] == KEYS
    for i in items:
        assert i["state"] in ("ok", "warn", "missing")
        assert i["title"] and i["detail"].endswith(".")
        assert i["action"] is None or set(i["action"]) == {"kind", "label"}
    return {i["key"]: i for i in items}


def test_readiness_on_a_fresh_mac(client: TestClient, offline: list[str]) -> None:
    items = readiness(client)
    assert items["engine"]["state"] == "ok"
    assert items["cpu"]["state"] == "ok"
    assert items["cpu"]["action"]["kind"] == "new_experiment"
    reader = items["reader"]
    assert reader["state"] == "missing"
    assert reader["action"] == {"kind": "open_models", "label": "Choose a reader model"}
    gpu = items["gpu_host"]
    assert gpu["state"] == "warn" and gpu["action"]["kind"] == "open_compute"
    assert "No GPU box" in gpu["detail"]
    assert items["keychain"]["state"] == "ok"
    assert offline == []


def test_readiness_reader_set_but_not_served(client: TestClient) -> None:
    client.patch("/profile", json={"default_model": "qwen-reader"})
    reader = readiness(client)["reader"]
    assert reader["state"] == "missing" and "qwen-reader" in reader["detail"]
    assert reader["action"] == {"kind": "open_models", "label": "Choose a reader model"}


def test_readiness_reader_at_two_revisions_points_to_models(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two services run the reader at different revisions (an upgrade): the router can't
    choose. Readiness says so, with Models as the way out, not 'nothing runs it'."""
    client.patch("/profile", json={"default_model": "qwen-reader"})
    router = ctx_of(client).router
    known = [{"id": f"svc_{rev}", "host_id": "local", "state": "ready",
              "engine": "ollama", "model": "qwen-reader", "revision": rev * 12}
             for rev in "ab"]  # fmt: skip
    monkeypatch.setattr(router, "_known", lambda: known)
    reader = readiness(client)["reader"]
    assert reader["state"] == "warn", reader
    assert reader["action"] == {"kind": "open_models", "label": "Open Models"}
    assert "No model service runs" not in reader["detail"]
    assert "several revisions" in reader["detail"] and "qwen-reader@" in reader["detail"]
    known.pop()  # one revision left, not answering yet
    assert readiness(client)["reader"]["state"] == "warn"
    known.clear()  # nothing runs it: choose (or start) a reader in Models
    reader = readiness(client)["reader"]
    assert reader["state"] == "missing"
    assert reader["action"] == {"kind": "open_models", "label": "Choose a reader model"}


def test_readiness_reader_served(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.patch("/profile", json={"default_model": "qwen-reader"})
    router = ctx_of(client).router
    endpoint = SimpleNamespace(service_id="svc_reader")
    monkeypatch.setattr(router, "resolve", lambda name: (name, name, lambda e: True))
    monkeypatch.setattr(router, "endpoints", lambda: [endpoint])
    models = [{"newton": {"services": [{"id": "svc_reader", "routable": True}]}}]
    monkeypatch.setattr(router, "models", lambda: models)
    reader = readiness(client)["reader"]
    assert reader["state"] == "ok" and reader["action"] is None
    models[0]["newton"]["services"][0]["routable"] = False  # paused for a timed run
    reader = readiness(client)["reader"]
    assert reader["state"] == "warn" and reader["action"]["kind"] == "open_models"


def test_readiness_never_shows_the_network(client: TestClient) -> None:
    """Papers always come from the internet: whether it answered last time is internal
    (/health.network), not something the readiness card shows."""
    ctx = ctx_of(client)
    ctx.network = SimpleNamespace(
        snapshot=lambda: {"state": "offline", "since": 1.0, "detail": "arXiv couldn't be reached"},
        is_offline=lambda: True,
    )  # fmt: skip
    assert "network" not in readiness(client)


def test_readiness_numpy_missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: False)
    cpu = readiness(client)["cpu"]
    assert cpu["state"] == "missing" and cpu["action"] is None
    assert cpu["detail"].startswith("numpy is missing from Newton's runtime")


def test_readiness_gpu_host_down_and_keychain_locked(client: TestClient) -> None:
    ctx = ctx_of(client)
    t = 1.0
    ctx.db.insert("hosts", {
        "id": "host_gb10", "name": "gb10", "kind": "ssh", "ssh_target": "gb10",
        "token_ref": "host-token-gb10", "status": "error:unresolvable",
        "last_error": "gb10 can't be resolved", "created_at": t, "updated_at": t,
    })  # fmt: skip
    gpu = readiness(client)["gpu_host"]
    assert gpu["state"] == "warn" and gpu["action"]["kind"] == "open_compute"
    assert "gb10 isn't reachable (gb10 can't be resolved)" in gpu["detail"]
    ctx.db.execute("UPDATE hosts SET status = 'online' WHERE id = 'host_gb10'")
    assert readiness(client)["gpu_host"]["state"] == "ok"

    ctx.profile._key = None  # noqa: SLF001 - the key couldn't be read at startup
    keychain = readiness(client)["keychain"]
    assert keychain["state"] == "missing" and keychain["action"]["kind"] == "open_settings"
