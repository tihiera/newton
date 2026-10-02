"""Live SV1 check on a real GPU host. Opt-in:

    NEWTON_TEST_SSH_HOST=<alias> NEWTON_TEST_SERVICE_MODEL=llama3.2:3b \\
    NEWTON_TEST_SERVICE_DIGEST=<manifest digest prefix> scripts/test.sh -m live

Through agentd's SSH runner and the worker API: start a Newton-owned Ollama service
(its own port and model store; pulls the model, about 2 GB for llama3.2:3b), wait
until it is ready and resident, ask it something on the host, then stop it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, make_settings
from fastapi.testclient import TestClient
from newton_agentd.app import create_app

HOST = os.environ.get("NEWTON_TEST_SSH_HOST")
MODEL = os.environ.get("NEWTON_TEST_SERVICE_MODEL", "llama3.2:3b")
DIGEST = os.environ.get("NEWTON_TEST_SERVICE_DIGEST", "a80c4f17acd5")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not HOST, reason="NEWTON_TEST_SSH_HOST not set"),
]


def test_live_ollama_service(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        host = client.post("/hosts", json={"name": "live", "ssh_target": HOST,
                                           "gpu_support": "off"}).json()  # fmt: skip
        assert client.post(f"/hosts/{host['id']}/bootstrap", timeout=900).status_code == 200
        ctx = client.app.state.ctx  # type: ignore[attr-defined]
        portal = client.portal  # type: ignore[attr-defined]

        def worker(method: str, path: str, **kwargs: Any) -> Any:
            async def go() -> Any:
                runner = await ctx.hosts.runner(host["id"])
                return await runner._call(lambda c: c._json(method, path, **kwargs))

            return portal.call(go)

        spec = {"service_id": "sv1-live", "engine": "ollama", "model": MODEL,
                "revision": DIGEST, "memory_gb": 4, "parallel": 4, "context_length": 4096,
                "startup_timeout_s": 1800}  # fmt: skip
        admission = worker("POST", "/services/admission", json=spec)
        print("admission:", admission)
        assert admission["ok"], admission
        started = worker("POST", "/services", json=spec)
        print("started:", started)
        deadline = time.time() + 1800
        status = started
        while status["state"] not in ("ready", "failed", "stopped", "lost"):
            assert time.time() < deadline, status
            time.sleep(5)
            status = worker("GET", "/services/sv1-live")
        print("status:", status)
        try:
            assert status["state"] == "ready" and status["healthy"] is True, status
            port = int(status["port"])
            client_ssh = ctx.hosts.ssh_client(ctx.hosts.get_row(host["id"]))
            question = (
                '{"model":"' + MODEL + '","messages":[{"role":"user",'
                '"content":"Reply with the single word: ready"}],"max_tokens":8}'
            )
            command = (
                f"curl -s -m 60 http://127.0.0.1:{port}/v1/chat/completions "
                f"-H 'Content-Type: application/json' -d '{question}'"
            )
            out = portal.call(client_ssh.run, command)
            print("answer:", out[-400:])
            assert '"choices"' in out
        finally:
            worker("POST", "/services/sv1-live/stop")
            deadline = time.time() + 120
            while worker("GET", "/services/sv1-live")["state"] not in ("stopped", "failed"):
                assert time.time() < deadline
                time.sleep(2)
        assert worker("GET", "/services/sv1-live")["state"] == "stopped"


def test_live_model_used_from_this_mac(tmp_path: Path) -> None:
    """SV2 acceptance: a model on the GPU host, answered on this Mac through agentd's
    private forward, with the service managed entirely through the API."""
    import httpx

    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        host = client.post("/hosts", json={"name": "live", "ssh_target": HOST,
                                           "gpu_support": "off"}).json()  # fmt: skip
        assert client.post(f"/hosts/{host['id']}/bootstrap", timeout=900).status_code == 200
        r = client.post("/services", json={
            "host_id": host["id"], "name": "small-llm",
            "settings": {"engine": "ollama", "model": MODEL, "revision": DIGEST,
                         "memory_gb": 4, "parallel": 4, "context_length": 4096},
        })  # fmt: skip
        assert r.status_code == 201, r.text
        svc = r.json()
        print("created:", svc["state"])
        if svc["state"] == "awaiting_approval":  # first time on this host: a download
            approval = client.get("/approvals?status=pending").json()[0]
            print("approval:", approval["title"])
            client.post(f"/approvals/{approval['id']}/approve")
        try:
            t0 = time.time()
            ready = None
            while time.time() - t0 < 1800:
                ready = client.get(f"/services/{svc['id']}").json()
                if ready["state"] in ("failed", "lost") or (ready.get("endpoint") or {}).get(
                    "reachable"
                ):
                    break
                time.sleep(3)
            print("service:", {k: ready.get(k) for k in ("state", "remote_port", "local_port",
                                                          "endpoint", "error")})  # fmt: skip
            assert ready and ready["state"] == "ready", ready
            print(f"ready and reachable on this Mac after {time.time() - t0:.0f} s")
            creds = client.get(f"/services/{svc['id']}/credentials").json()
            t1 = time.time()
            answer = httpx.post(
                f"{creds['base_url']}/chat/completions",
                json={"model": creds["model"], "max_tokens": 20,
                      "messages": [{"role": "user", "content": "In one word: is 2+2=4?"}]},
                timeout=120,
            )  # fmt: skip
            print(f"answer in {time.time() - t1:.2f} s:", answer.json()["choices"][0])
            assert answer.status_code == 200
        finally:
            client.post(f"/services/{svc['id']}/stop")
            deadline = time.time() + 120
            while client.get(f"/services/{svc['id']}").json()["state"] not in (
                "stopped",
                "failed",
                "lost",
                "cancelled",
            ):
                assert time.time() < deadline
                time.sleep(2)
        print("final:", client.get(f"/services/{svc['id']}").json()["state"])


MAC_MODEL = os.environ.get("NEWTON_TEST_MAC_MODEL", "qwen2.5:0.5b")
MAC_DIGEST = os.environ.get("NEWTON_TEST_MAC_DIGEST", "a8b0c5157701")


def test_live_router_spark_and_mac_in_parallel(tmp_path: Path) -> None:
    """SV3 acceptance, through the router only (its key, its /v1):
    - a model on the Spark and a small one on this Mac answer in parallel;
    - a timed CUDA benchmark on the Spark pauses the Spark's inference (refused with a
      clear 503) while the Mac keeps answering, and the report says so;
    - the Spark answers again once the benchmark is done."""
    import threading

    import httpx
    from conftest import wait_for
    from test_live_ssh import run_performance

    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        spark = client.post("/hosts", json={"name": "spark", "ssh_target": HOST}).json()
        assert client.post(f"/hosts/{spark['id']}/bootstrap", timeout=900).status_code == 200
        services: list[dict[str, Any]] = []
        try:  # whatever was started gets stopped, even if setup fails half way
            for host_id, name, model, digest, memory in (
                (spark["id"], "spark-llm", MODEL, DIGEST, 4),
                ("local", "mac-llm", MAC_MODEL, MAC_DIGEST, 1),  # 0.4 GB of weights
            ):
                r = client.post("/services", json={"host_id": host_id, "name": name, "settings": {
                    "engine": "ollama", "model": model, "revision": digest, "memory_gb": memory,
                    "parallel": 2, "context_length": 4096}})  # fmt: skip
                assert r.status_code == 201, r.text
                services.append(r.json())
            for approval in client.get("/approvals?status=pending").json():
                print("approving:", approval["title"])  # first time here: a download
                client.post(f"/approvals/{approval['id']}/approve")
            for svc in services:
                up = wait_for(lambda s=svc: client.get(f"/services/{s['id']}").json(),
                              lambda s: s["state"] in ("failed", "lost")
                              or (s.get("endpoint") or {}).get("reachable"),
                              timeout=1800, interval=3)  # fmt: skip
                assert up["state"] == "ready", up
            creds = client.get("/router/credentials").json()
            key = {"Authorization": f"Bearer {creds['api_key']}"}  # inference only
            spark_model = MODEL if ":" in MODEL else f"{MODEL}:latest"

            def ask(model: str, text: str) -> httpx.Response:
                return client.post("/v1/chat/completions", headers=key, timeout=300, json={
                    "model": model, "max_tokens": 40,
                    "messages": [{"role": "user", "content": text}]})  # fmt: skip

            ask(spark_model, "warm up")
            ask(MAC_MODEL, "warm up")
            spans: dict[str, list[tuple[float, float]]] = {"spark": [], "mac": []}

            def timed(where: str, model: str, i: int) -> None:
                t0 = time.time()
                r = ask(model, f"Count from {i} to {i + 5}, digits only.")
                assert r.status_code == 200, r.text
                spans[where].append((t0, time.time()))

            threads = [threading.Thread(target=timed, args=(w, m, i)) for i in range(4)
                       for w, m in (("spark", spark_model), ("mac", MAC_MODEL))]  # fmt: skip
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            overlap = any(a0 < b1 and b0 < a1 for a0, a1 in spans["spark"]
                          for b0, b1 in spans["mac"])  # fmt: skip
            print("spans:", {k: [round(e - s, 2) for s, e in v] for k, v in spans.items()})
            assert overlap, spans  # served at the same time, on two machines

            # A timed CUDA benchmark on the Spark: its inference pauses, the Mac's doesn't.
            seen: dict[str, Any] = {}

            def watch() -> None:
                ctx = client.app.state.ctx  # type: ignore[attr-defined]
                deadline = time.time() + 1800
                while time.time() < deadline and "spark" not in seen:
                    if ctx.router.lease_holder(spark["id"]):
                        seen["spark"] = ask(spark_model, "are you there?")
                        seen["mac"] = ask(MAC_MODEL, "are you there?")
                    time.sleep(0.5)

            watcher = threading.Thread(target=watch)
            watcher.start()
            report = run_performance(client, spark["id"], "cuda", "float64")
            watcher.join()
            print("during the benchmark:", seen["spark"].status_code, seen["spark"].json(),
                  "| mac:", seen["mac"].status_code)  # fmt: skip
            assert seen["spark"].status_code == 503
            assert seen["spark"].json()["error"]["code"] == "device_leased"
            assert seen["mac"].status_code == 200
            assert "paused on this host" in report
            after = wait_for(lambda: ask(spark_model, "and now?").status_code,
                             lambda s: s == 200, timeout=120)  # fmt: skip
            assert after == 200
            rows = client.get("/router/requests?limit=500").json()
            print("router log:", len(rows), "requests,",
                  sum(1 for r in rows if r["status"] == 200), "ok")  # fmt: skip
        finally:
            for svc in services:
                client.post(f"/services/{svc['id']}/stop")
            for svc in services:
                wait_for(lambda s=svc: client.get(f"/services/{s['id']}").json()["state"],
                         lambda st: st in ("stopped", "failed", "lost", "cancelled"),
                         timeout=180)  # fmt: skip
