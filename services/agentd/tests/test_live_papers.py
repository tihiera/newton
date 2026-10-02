"""B4 live: a real arXiv physics paper, read by a model on the GB10 through the
router, becomes a card and (when its method maps onto the IR) an experiment."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from conftest import AUTH, make_settings, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app

HOST = os.environ.get("NEWTON_TEST_SSH_HOST")
PAPER = os.environ.get("NEWTON_TEST_ARXIV", "2110.03044")
MODEL = os.environ.get("NEWTON_TEST_PAPER_MODEL", "llama3.2:3b")
DIGEST = os.environ.get("NEWTON_TEST_PAPER_DIGEST", "a80c4f17acd5")
pytestmark = [pytest.mark.live, pytest.mark.skipif(not HOST, reason="NEWTON_TEST_SSH_HOST not set")]


def test_live_arxiv_paper_to_card_to_experiment(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        spark = client.post("/hosts", json={"name": "spark", "ssh_target": HOST,
                                            "gpu_support": "off"}).json()  # fmt: skip
        assert client.post(f"/hosts/{spark['id']}/bootstrap", timeout=900).status_code == 200
        svc = client.post("/services", json={"host_id": spark["id"], "name": "reader", "settings": {
            "engine": "ollama", "model": MODEL, "revision": DIGEST, "memory_gb": 6,
            "parallel": 1, "context_length": 16384}}).json()  # fmt: skip
        try:
            for approval in client.get("/approvals?status=pending").json():
                client.post(f"/approvals/{approval['id']}/approve")
            wait_for(lambda: client.get(f"/services/{svc['id']}").json(),
                     lambda s: (s.get("endpoint") or {}).get("reachable"),
                     timeout=1800, interval=3)  # fmt: skip
            client.patch("/profile", json={"default_model": MODEL})
            t0 = time.time()
            item = client.post("/research/ingest", json={"ref": PAPER}).json()
            item = wait_for(lambda: client.get(f"/research/items/{item['id']}").json(),
                            lambda i: i["state"] in ("carded", "failed"), timeout=900,
                            interval=2)  # fmt: skip
            data = item["data"]
            print(f"\n{item['state']} in {time.time() - t0:.0f} s: {item['title']}")
            print("text:", data.get("text"))
            print("card:", json.dumps(data.get("card"), indent=1))
            print("scheme:", data.get("scheme_note"), json.dumps(data.get("scheme_ir")))
            print("extraction:", data.get("extraction"))
            assert item["state"] == "carded", data.get("error")
            assert data["card"]["summary"]
            if data.get("scheme_ir"):
                r = client.post(f"/research/items/{item['id']}/propose",
                                json={"host_id": "local", "backend": "cpu"})  # fmt: skip
                assert r.status_code == 201, r.text
                approval = next(a for a in client.get("/approvals?status=pending").json()
                                if a["subject_id"] == r.json()["id"])  # fmt: skip
                client.post(f"/approvals/{approval['id']}/approve")
                exp_id = r.json()["id"]
                done = wait_for(lambda: client.get(f"/experiments/{exp_id}").json(),
                                lambda e: e["state"] in ("reported", "failed"),
                                timeout=600)  # fmt: skip
                print("verdict:", json.dumps(done["evaluation"]["verdicts"], indent=1)[:3000])
                assert done["state"] == "reported"
        finally:
            client.post(f"/services/{svc['id']}/stop")
            ended = ("stopped", "failed", "lost", "cancelled")
            wait_for(lambda: client.get(f"/services/{svc['id']}").json()["state"],
                     lambda s: s in ended, timeout=180)  # fmt: skip


def test_live_research_loop_poll(tmp_path: Path) -> None:
    """B5 live: a goal polls arXiv for real; the model on the GB10 triages and cards."""
    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        spark = client.post("/hosts", json={"name": "spark", "ssh_target": HOST,
                                            "gpu_support": "off"}).json()  # fmt: skip
        assert client.post(f"/hosts/{spark['id']}/bootstrap", timeout=900).status_code == 200
        svc = client.post("/services", json={"host_id": spark["id"], "name": "reader", "settings": {
            "engine": "ollama", "model": MODEL, "revision": DIGEST, "memory_gb": 6,
            "parallel": 1, "context_length": 16384}}).json()  # fmt: skip
        try:
            for approval in client.get("/approvals?status=pending").json():
                client.post(f"/approvals/{approval['id']}/approve")
            wait_for(lambda: client.get(f"/services/{svc['id']}").json(),
                     lambda s: (s.get("endpoint") or {}).get("reachable"),
                     timeout=1800, interval=3)  # fmt: skip
            client.patch("/profile", json={"default_model": MODEL})
            goal = client.post("/goals", json={
                "title": "Higher-order advection schemes that beat upwind",
                "description": "Flux limiters and TVD schemes for linear advection.",
                "keywords": ["flux limiter", "TVD scheme"], "auto_propose": True,
            }).json()  # fmt: skip
            t0 = time.time()
            summary = client.post(f"/goals/{goal['id']}/poll").json()
            print(f"\npoll in {time.time() - t0:.0f} s:", json.dumps(summary, indent=1))
            for item in client.get(f"/research/items?goal_id={goal['id']}").json():
                d = item["data"]
                print(f"- {item['external_id']} [{item['state']}] {item['title'][:70]}")
                print("   triage:", (d.get("triage") or {}).get("why"))
                if d.get("card"):
                    print("   method:", d["card"]["method"], "|", d.get("scheme_note"))
            assert summary["found"] > 0 and "error" not in summary
            assert summary["relevant"] + summary["dismissed"] + len(summary["skipped"]) > 0
        finally:
            client.post(f"/services/{svc['id']}/stop")
            ended = ("stopped", "failed", "lost", "cancelled")
            wait_for(lambda: client.get(f"/services/{svc['id']}").json()["state"],
                     lambda s: s in ended, timeout=180)  # fmt: skip
