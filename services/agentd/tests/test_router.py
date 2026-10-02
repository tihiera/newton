"""SV3: the router. One OpenAI-compatible /v1 on agentd for every model service,
routing by model (no roles), a slot limit per service, provenance on every answer,
the router key (inference only), and device leases for timed jobs.

Most tests run the fake engine on the local host. Disconnect tests and the OpenAI
SDK need a real HTTP server: agentd under uvicorn on a free port.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
import uvicorn
from conftest import AUTH, TOKEN, make_settings, wait_for, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.orchestration import scheduler as scheduler_mod
from test_services_api import FAKE, stop_services, wait_state

SENTINEL = "zebra-sentinel-4417"


@pytest.fixture(autouse=True)
def quick_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    monkeypatch.setenv("NEWTON_SERVICE_STOP_GRACE", "2")


@pytest.fixture
def local(client: TestClient) -> Iterator[TestClient]:
    yield client
    stop_services(client)


class Live:
    """agentd under uvicorn: real sockets, real disconnects."""

    def __init__(self, tmp_path: Path) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.app = create_app(make_settings(tmp_path, port=port))
        self.ctx = self.app.state.ctx
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.started, bool, timeout=30)
        self.base = f"http://127.0.0.1:{port}"
        self.http = httpx.Client(base_url=self.base, headers=AUTH, timeout=60, trust_env=False)

    def stop(self) -> None:
        for s in self.http.get("/services").json():
            if s["state"] in ("starting", "ready", "draining"):
                self.http.post(f"/services/{s['id']}/stop")
        wait_for(lambda: [s for s in self.http.get("/services").json()
                          if s["state"] in ("starting", "ready", "draining", "stopping")],
                 lambda active: not active, timeout=30)  # fmt: skip
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=30)


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    server = Live(tmp_path)
    try:
        yield server
    finally:
        server.stop()


def ready(client: TestClient | httpx.Client, name: str = "echo", **settings: Any
          ) -> dict[str, Any]:  # fmt: skip
    r = client.post("/services", json={"host_id": "local", "name": name,
                                       "settings": {**FAKE, **settings}})  # fmt: skip
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    out: dict[str, Any] = wait_for(lambda: client.get(f"/services/{sid}").json(),
                                   lambda s: s["state"] == "ready"
                                   and (s.get("endpoint") or {}).get("reachable"),
                                   timeout=60)  # fmt: skip
    return out


def ask(client: TestClient | httpx.Client, model: str = "fake/echo", text: str = "hi",
        **extra: Any) -> httpx.Response:  # fmt: skip
    return client.post("/v1/chat/completions", json={
        "model": model, "messages": [{"role": "user", "content": text}], **extra})  # fmt: skip


def engine_stats(svc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = httpx.get(
        f"http://127.0.0.1:{svc['remote_port']}/stats", trust_env=False
    ).json()
    return out


# -- routing and provenance -----------------------------------------------------------


def test_routes_by_model_with_provenance_and_no_content_stored(local: TestClient) -> None:
    svc = ready(local)
    r = ask(local, text=f"say {SENTINEL}")
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == f"echo: say {SENTINEL}"
    assert r.headers["X-Newton-Service"] == svc["id"] and r.headers["X-Newton-Host"] == "local"
    assert r.headers["X-Newton-Engine"] == "fake" and r.headers["X-Newton-Model"] == "fake/echo"
    rid = r.headers["X-Newton-Request-Id"]
    row = next(x for x in local.get("/router/requests").json() if x["id"] == rid)
    assert row["status"] == 200 and row["service_id"] == svc["id"] and row["attempts"] == 1
    assert row["prompt_tokens"] == 2 and row["completion_tokens"] == 3
    assert row["first_byte_ms"] is not None and row["duration_ms"] >= row["first_byte_ms"]
    # Upstream errors too: only a code is kept, never the engine's text.
    bad = local.post("/v1/embeddings", json={"model": "fake/echo", "input": SENTINEL})
    assert bad.status_code == 200 and len(bad.json()["data"][0]["embedding"]) == 8
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    stored = b"".join(p.read_bytes() for p in Path(ctx.settings.data_dir).glob("newton.db*"))
    assert SENTINEL.encode() not in stored
    completion = local.post("/v1/completions", json={"model": "fake/echo", "prompt": "p"})
    assert completion.json()["choices"][0]["text"] == "echo: p"


def test_models_names_ids_and_the_profile_default(local: TestClient) -> None:
    svc = ready(local)
    models = local.get("/v1/models").json()["data"]
    assert [m["id"] for m in models] == ["fake/echo"]
    assert models[0]["newton"]["services"][0]["routable"] is True
    assert ask(local, svc["id"]).headers["X-Newton-Service"] == svc["id"]  # a service id
    missing = ask(local, "llama9:70b")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"
    assert "fake/echo" in missing.json()["error"]["message"]  # what is available
    assert ask(local, "default").json()["error"]["code"] == "model_not_found"
    profile = {"display_name": "Tito’s Mac", "default_model": "fake/echo"}
    assert local.patch("/profile", json=profile).status_code == 200
    r = ask(local, "default")
    assert r.status_code == 200 and r.headers["X-Newton-Model"] == "fake/echo"
    row = local.get("/router/requests?limit=1").json()[0]
    assert row["model_requested"] == "default -> fake/echo"
    assert local.get("/profile").json()["display_name"] == "Tito’s Mac"
    long = ask(local, "x" * 300)
    assert long.status_code == 400 and long.json()["error"]["code"] == "invalid_model"


def test_two_revisions_are_ambiguous_unless_pinned(local: TestClient) -> None:
    a = ready(local, "a", revision="a" * 12)
    b = ready(local, "b", revision="b" * 12)
    r = ask(local)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ambiguous_model"
    assert ask(local, "fake/echo@aaaa").headers["X-Newton-Service"] == a["id"]
    assert ask(local, f"fake/echo@{'b' * 12}").headers["X-Newton-Service"] == b["id"]


def test_free_text_names_never_reach_headers(local: TestClient) -> None:
    svc = ready(local, "Tito’s model ✓")  # headers must stay latin-1: ids only
    r = ask(local)
    assert r.status_code == 200 and r.headers["X-Newton-Service"] == svc["id"]
    assert r.headers["X-Newton-Host"] == "local"


# -- keys ---------------------------------------------------------------------------------


def test_the_router_key_is_inference_only(local: TestClient) -> None:
    ready(local)
    creds = local.get("/router/credentials").json()
    assert creds["base_url"].endswith("/v1") and creds["api_key"] != TOKEN
    key = {"Authorization": f"Bearer {creds['api_key']}"}
    assert ask_with(local, key).status_code == 200
    assert local.get("/v1/models", headers=key).status_code == 200
    assert local.get("/hosts", headers=key).status_code == 401  # no admin
    assert local.post("/router/credentials/rotate", headers=key).status_code == 401
    nokey = ask_with(local, {"Authorization": "Bearer nope"})
    assert nokey.status_code == 401 and nokey.json()["error"]["code"] == "invalid_api_key"
    rotated = local.post("/router/credentials/rotate").json()["api_key"]
    assert ask_with(local, key).status_code == 401
    assert ask_with(local, {"Authorization": f"Bearer {rotated}"}).status_code == 200
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    assert ctx.secrets.get("router-key") == rotated  # in the secret store, not SQLite
    stored = b"".join(p.read_bytes() for p in Path(ctx.settings.data_dir).glob("newton.db*"))
    assert rotated.encode() not in stored


def ask_with(client: TestClient, headers: dict[str, str]) -> httpx.Response:
    return client.post("/v1/chat/completions", headers=headers, json={
        "model": "fake/echo", "messages": [{"role": "user", "content": "k"}]})  # fmt: skip


def test_an_engine_refusing_its_key_is_not_the_callers_fault(local: TestClient) -> None:
    svc = ready(local)
    ctx = local.app.state.ctx  # type: ignore[attr-defined]

    async def wrong_key() -> None:
        fut = asyncio.get_running_loop().create_future()
        fut.set_result("not-the-engines-key")
        ctx.router._keys[svc["id"]] = fut

    local.portal.call(wrong_key)  # type: ignore[attr-defined]
    r = ask(local)
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_auth_failed"
    unreachable = [e for e in local.get(f"/events?entity_id={svc['id']}").json()
                   if e["kind"] == "unreachable"]  # fmt: skip
    assert unreachable and unreachable[-1]["data"] == {"by": "router"}  # re-checked now


# -- slots, queue, failover ------------------------------------------------------------------


def test_each_service_takes_at_most_parallel_requests(local: TestClient) -> None:
    one = ready(local, "one", parallel=1, fake={"reply_delay_s": 0.6})
    two = ready(local, "two", parallel=1, fake={"reply_delay_s": 0.6})
    with ThreadPoolExecutor(6) as pool:
        replies = list(pool.map(lambda i: ask(local, text=str(i)), range(6)))
    assert all(r.status_code == 200 for r in replies)
    assert {r.headers["X-Newton-Service"] for r in replies} == {one["id"], two["id"]}
    assert engine_stats(one)["max_active"] == 1 and engine_stats(two)["max_active"] == 1
    rows = local.get("/router/requests?limit=6").json()
    assert max(r["queued_ms"] for r in rows) > 300  # some waited their turn


def test_queue_limits_answer_like_openai(local: TestClient) -> None:
    ready(local, parallel=1, fake={"reply_delay_s": 3})
    settings = local.app.state.ctx.settings  # type: ignore[attr-defined]
    settings.router_queue_timeout = 0.5
    settings.router_max_queue = 1
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    with ThreadPoolExecutor(3) as pool:
        first = pool.submit(ask, local)
        wait_for(router.in_flight, lambda n: n == 1, timeout=5)
        second = pool.submit(ask, local)
        wait_for(router.waiting, lambda n: n == 1, timeout=5)
        third = pool.submit(ask, local)
        results = [first.result(), second.result(), third.result()]
    assert results[0].status_code == 200
    timeout = results[1]
    assert timeout.status_code == 503 and timeout.json()["error"]["code"] == "queue_timeout"
    assert timeout.headers["x-should-retry"] == "false"
    full = results[2]
    assert full.status_code == 429 and full.json()["error"]["code"] == "queue_full"
    assert int(full.headers["Retry-After"]) <= 60


class Sink:
    """How a dead engine behind an `ssh -L` looks: the port accepts, then closes."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.close()


def point(ctx: Any, service_id: str, port: int, first: bool = False) -> None:
    ctx.services.on_change = lambda: None  # keep the manager from repairing it mid-test
    ctx.services.reprobe = lambda sid: None
    ctx.services._ensure_endpoint = _no_endpoint
    ctx.db.execute("UPDATE services SET local_port = ? WHERE id = ?", (port, service_id))
    if first:  # ties go to the oldest
        ctx.db.execute("UPDATE services SET created_at = 0 WHERE id = ?", (service_id,))


async def _no_endpoint(*args: Any) -> None:
    return None


def test_a_service_that_closes_the_connection_is_skipped(local: TestClient) -> None:
    good = ready(local, "good")
    bad = ready(local, "bad")
    sink = Sink()
    point(local.app.state.ctx, bad["id"], sink.port, first=True)  # type: ignore[attr-defined]
    r = ask(local)
    assert r.status_code == 200 and r.headers["X-Newton-Service"] == good["id"]
    row = local.get("/router/requests?limit=1").json()[0]
    assert row["attempts"] == 2  # tried the bad one first, then the good one


def test_when_no_service_answers_it_is_a_502_at_once(local: TestClient) -> None:
    only = ready(local)
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    ctx.settings.router_queue_timeout = 30
    point(ctx, only["id"], Sink().port)
    t0 = time.time()
    r = ask(local)
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unreachable"
    assert time.time() - t0 < 5  # not after the queue timeout
    with socket.socket() as s:  # a port nobody listens on: that one is down
        s.bind(("127.0.0.1", 0))
        refused = s.getsockname()[1]
    point(ctx, only["id"], refused)
    assert ask(local).json()["error"]["code"] == "upstream_unreachable"
    assert ctx.services.reachable(only["id"]) is False  # marked down: re-probed now
    assert ctx.router.in_flight() == 0


# -- streaming ----------------------------------------------------------------------------------


def test_streams_are_relayed_event_by_event(local: TestClient) -> None:
    ready(local)
    with local.stream("POST", "/v1/chat/completions", json={
        "model": "fake/echo", "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "one two"}],
    }) as r:  # fmt: skip
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = [line for line in r.iter_lines() if line.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    text = "".join(
        (json.loads(e[6:])["choices"] or [{}])[0].get("delta", {}).get("content", "")
        for e in events[:-1]
    )
    assert text == "echo: one two"
    row = local.get("/router/requests?limit=1").json()[0]
    assert row["stream"] == 1 and row["status"] == 200 and row["error"] is None
    assert row["completion_tokens"] == 3  # "echo:", "one", "two"


def test_the_openai_sdk_works_against_the_router(live: Live) -> None:
    ready(live.http)
    key = live.http.get("/router/credentials").json()["api_key"]
    sdk = openai.OpenAI(base_url=f"{live.base}/v1", api_key=key, max_retries=0,
                        http_client=httpx.Client(trust_env=False))  # fmt: skip
    reply = sdk.chat.completions.create(model="fake/echo",
                                        messages=[{"role": "user", "content": "sdk"}])  # fmt: skip
    assert reply.choices[0].message.content == "echo: sdk"
    stream = sdk.chat.completions.create(model="fake/echo", stream=True,
                                         messages=[{"role": "user", "content": "a b"}])  # fmt: skip
    assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) == "echo: a b"
    assert sdk.embeddings.create(model="fake/echo", input=["x", "y"]).data[1].index == 1
    with pytest.raises(openai.NotFoundError) as e:
        sdk.chat.completions.create(model="nope:1b", messages=[{"role": "user", "content": "x"}])
    assert e.value.code == "model_not_found"
    with pytest.raises(openai.AuthenticationError):
        openai.OpenAI(base_url=f"{live.base}/v1", api_key="wrong", max_retries=0,
                      http_client=httpx.Client(trust_env=False)).models.list()  # fmt: skip
    assert [m.id for m in sdk.models.list()] == ["fake/echo"]


# -- clients that go away -------------------------------------------------------------------------


def test_a_client_that_leaves_frees_its_slot(live: Live) -> None:
    svc = ready(live.http, parallel=1, fake={"reply_delay_s": 3})
    router = live.ctx.router
    with pytest.raises(httpx.ReadTimeout):  # mid-answer
        ask(httpx.Client(base_url=live.base, headers=AUTH, timeout=0.5, trust_env=False))
    wait_for(router.in_flight, lambda n: n == 0, timeout=5)
    started = time.time()
    holder = threading.Thread(target=ask, args=(live.http,))
    holder.start()  # takes the only slot for 3 s
    wait_for(router.in_flight, lambda n: n == 1, timeout=5)
    with pytest.raises(httpx.ReadTimeout):  # while waiting in line
        ask(httpx.Client(base_url=live.base, headers=AUTH, timeout=0.5, trust_env=False))
    wait_for(router.waiting, lambda n: n == 0, timeout=5)
    holder.join()
    assert time.time() - started < 6  # the abandoned one never ran
    rows = live.http.get("/router/requests?limit=10").json()
    assert sum(1 for r in rows if r["status"] == 499) == 2
    assert engine_stats(svc)["count"] <= 2


def test_a_stream_the_client_drops_frees_its_slot(live: Live) -> None:
    ready(live.http, parallel=1, fake={"reply_delay_s": 4})
    router = live.ctx.router
    with httpx.Client(base_url=live.base, headers=AUTH, timeout=30, trust_env=False) as c, \
            c.stream("POST", "/v1/chat/completions", json={
                "model": "fake/echo", "stream": True,
                "messages": [{"role": "user", "content": "a b c d e f"}]}) as r:  # fmt: skip
        next(r.iter_lines())  # the first event, then hang up
    wait_for(router.in_flight, lambda n: n == 0, timeout=5)
    row = wait_for(lambda: live.http.get("/router/requests?limit=1").json()[0],
                   lambda x: x["finished_at"] is not None, timeout=10)  # fmt: skip
    assert row["error"] == "client_disconnected"


# -- device leases ---------------------------------------------------------------------------


@pytest.fixture
def alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every job counts as timed (selftests make convenient ones)."""
    monkeypatch.setattr(scheduler_mod, "_alone", lambda manifest: True)


def selftest(client: TestClient | httpx.Client, sleep: float = 1) -> str:
    r = client.post("/hosts/local/selftest", json={"sleep": sleep})
    assert r.status_code == 201, r.text
    job_id: str = r.json()["id"]
    return job_id


@pytest.mark.usefixtures("alone")
def test_a_timed_job_waits_for_inference_then_has_the_host(local: TestClient) -> None:
    ready(local, parallel=2, fake={"reply_delay_s": 2.5})
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    with ThreadPoolExecutor(2) as pool:
        busy = pool.submit(ask, local)
        wait_for(router.in_flight, lambda n: n == 1, timeout=5)
        job = selftest(local, sleep=2)
        waiting = wait_for(lambda: local.get(f"/jobs/{job}").json(),
                           lambda j: "waiting for 1 model request" in (j["error"] or ""),
                           timeout=10)  # fmt: skip
        assert waiting["state"] == "queued"
        refused = ask(local)  # the host is clearing: nothing new goes there
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "device_leased"
        assert "paused" in refused.json()["error"]["message"]
        assert busy.result().status_code == 200  # in flight: allowed to finish
    running = wait_job(local, job, {"running", "collecting", "succeeded"}, timeout=20)
    assert running["state"] != "queued"
    events = local.get(f"/events?entity_id={job}").json()
    lease = next(e["data"] for e in events if e["kind"] == "device_lease")
    assert lease["in_flight_at_start"] == 1 and lease["aborted"] == 0 and not lease["caveats"]
    wait_job(local, job, {"succeeded"}, timeout=30)
    wait_for(lambda: ask(local).status_code, lambda s: s == 200, timeout=10)  # resumed


@pytest.mark.usefixtures("alone")
def test_stragglers_are_aborted_and_the_timing_says_so(local: TestClient) -> None:
    ready(local, fake={"reply_delay_s": 30})
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    ctx.settings.lease_drain_timeout = 1.0
    with ThreadPoolExecutor(2) as pool:
        slow = pool.submit(ask, local)
        wait_for(ctx.router.in_flight, lambda n: n == 1, timeout=5)
        job = selftest(local, sleep=1)
        aborted = slow.result(timeout=20)
    assert aborted.status_code == 503 and aborted.json()["error"]["code"] == "device_leased"
    wait_job(local, job, {"succeeded"}, timeout=30)
    lease = next(e["data"] for e in local.get(f"/events?entity_id={job}").json()
                 if e["kind"] == "device_lease")  # fmt: skip
    assert lease["aborted"] == 1 and "aborted" in lease["caveats"][0]


@pytest.mark.usefixtures("alone")
def test_a_stream_aborted_for_a_timed_job_ends_with_an_error_event(live: Live) -> None:
    ready(live.http, fake={"reply_delay_s": 30})
    live.ctx.settings.lease_drain_timeout = 1.0
    started = time.time()
    with live.http.stream("POST", "/v1/chat/completions", json={
        "model": "fake/echo", "stream": True,
        "messages": [{"role": "user", "content": " ".join("w" * 40)}]}) as r:  # fmt: skip
        lines = r.iter_lines()
        assert next(lines).startswith("data: ")
        job = selftest(live.http, sleep=1)
        rest = [line for line in lines if line.startswith("data: ")]
    assert time.time() - started < 15  # cut short, not the full 30 s
    error = json.loads(rest[-1][6:])["error"]
    assert error["code"] == "device_leased" and "data: [DONE]" not in rest
    for line in rest[:-1]:
        json.loads(line[6:])  # every event before it whole
    wait_job(live.http, job, {"succeeded"}, timeout=30)  # type: ignore[arg-type]


@pytest.mark.usefixtures("alone")
def test_cancelling_a_waiting_timed_job_gives_the_host_back(local: TestClient) -> None:
    ready(local, fake={"reply_delay_s": 4})
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    with ThreadPoolExecutor(1) as pool:
        busy = pool.submit(ask, local)
        wait_for(router.in_flight, lambda n: n == 1, timeout=5)
        job = selftest(local)
        wait_for(lambda: router.lease_holder("local"), lambda h: h == job, timeout=10)
        local.post(f"/jobs/{job}/cancel")
        wait_for(lambda: router.lease_holder("local"), lambda h: h is None, timeout=10)
        assert busy.result().status_code == 200
    assert ask(local).status_code == 200


@pytest.mark.usefixtures("alone")
def test_no_model_starts_loading_while_a_timed_job_runs(local: TestClient) -> None:
    job = selftest(local, sleep=4)
    wait_job(local, job, {"running"}, timeout=20)
    r = local.post("/services", json={"host_id": "local", "name": "late", "settings": FAKE})
    held = wait_for(lambda: local.get(f"/services/{r.json()['id']}").json(),
                    lambda s: s["error"] and "timed run" in s["error"], timeout=10)  # fmt: skip
    assert held["state"] == "approved"
    wait_job(local, job, {"succeeded"}, timeout=30)
    wait_state(local, r.json()["id"], "ready")


def test_a_running_timed_job_keeps_its_host_across_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newton_agentd.secrets import MemorySecretStore

    monkeypatch.setattr(scheduler_mod, "_alone", lambda manifest: True)
    store = MemorySecretStore()
    with TestClient(create_app(make_settings(tmp_path), secret_store=store),
                    headers=AUTH) as first:  # fmt: skip
        job = selftest(first, sleep=6)
        wait_job(first, job, {"running"}, timeout=20)
    with TestClient(create_app(make_settings(tmp_path), secret_store=store),
                    headers=AUTH) as second:  # fmt: skip
        router = second.app.state.ctx.router  # type: ignore[attr-defined]
        assert router.lease_holder("local") == job  # before any request was served
        wait_job(second, job, {"succeeded"}, timeout=30)
        wait_for(lambda: router.lease_holder("local"), lambda h: h is None, timeout=10)


def test_lease_caveats_reach_the_timing(local: TestClient) -> None:
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    from newton_agentd.orchestration.state_machine import record_event

    record_event(ctx.db, "job", "job-x", "device_lease", {"caveats": ["2 aborted"]})
    results: dict[str, Any] = {"environment": {"timing_caveat": "Low Power Mode was on"}}
    ctx.scheduler._lease_caveats({"id": "job-x"}, results)
    assert results["environment"]["timing_caveat"] == "Low Power Mode was on; 2 aborted"


# -- review round: what must not regress -------------------------------------------------


def test_bad_bodies_never_cost_a_slot(local: TestClient) -> None:
    ready(local, parallel=1)
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    for raw in (b'{"model": "fake/echo", "temperature": NaN, "messages": []}',
                b'{"model": "fake/echo", "x": Infinity}', b"[" * 200000, b"[1, 2]"):  # fmt: skip
        r = local.post("/v1/chat/completions", content=raw,
                       headers={"Content-Type": "application/json"})  # fmt: skip
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json", raw[:30]
    lone = local.post("/v1/chat/completions", content=(
        b'{"model": "fake/echo", "messages": [{"role": "user", "content": "\\ud800"}]}'),
        headers={"Content-Type": "application/json"})  # fmt: skip
    assert lone.status_code == 200  # sent on as an escape, never an encoding error
    bad_stream = ask(local, stream="no")
    assert bad_stream.status_code == 400 and bad_stream.json()["error"]["code"] == "invalid_stream"
    assert router.in_flight() == 0 and ask(local).status_code == 200


def test_odd_tokens_and_a_locked_keychain(local: TestClient) -> None:
    ready(local)
    assert ask(local).status_code == 200  # the engine's key is read (and kept) now
    odd = {"Authorization": "Bearer \xff".encode("latin-1")}
    r = local.post("/v1/chat/completions", headers=odd,
                   json={"model": "fake/echo", "messages": []})  # fmt: skip
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    assert local.get("/hosts", headers=odd).status_code == 401
    profile = local.app.state.ctx.profile  # type: ignore[attr-defined]
    profile._key = None  # as if the keychain was locked at startup
    profile.secrets.get = lambda ref: (_ for _ in ()).throw(RuntimeError("locked"))
    locked = local.post("/v1/chat/completions", headers={"Authorization": "Bearer someone"},
                        json={"model": "fake/echo", "messages": []})  # fmt: skip
    assert locked.status_code == 503 and locked.json()["error"]["code"] == "secret_store"
    assert ask(local).status_code == 200  # the admin token never needs the keychain


def test_v1_speaks_openai_on_every_path(local: TestClient) -> None:
    ready(local)
    for method in ("PUT", "DELETE", "PATCH"):
        r = local.request(method, "/v1/chat/completions")
        assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
    assert local.get("/v1/nope").json()["error"]["code"] == "unknown_url"
    assert local.get("/v1/models/fake/echo").json()["id"] == "fake/echo"
    assert local.get("/v1/models/nope").json()["error"]["code"] == "model_not_found"
    assert local.patch("/profile", json={"default_model": "default"}).status_code == 422
    assert local.patch("/profile", json={"default_model": "x@zz"}).status_code == 422
    embed = local.post("/v1/embeddings", json={"model": "fake/echo", "input": "x",
                                               "stream": True})  # fmt: skip
    assert embed.status_code == 200 and embed.json()["data"][0]["index"] == 0  # not lost


def test_engine_errors_pass_through_without_their_text(local: TestClient) -> None:
    ready(local, "refuses", fake={"fail_status": 400})
    r = ask(local, text=SENTINEL)
    assert r.status_code == 400 and r.headers["X-Newton-Upstream-Status"] == "400"
    assert SENTINEL in r.text  # the caller sees the engine's own words...
    row = local.get("/router/requests?limit=1").json()[0]
    assert row["error"] == "upstream_400"
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    stored = b"".join(p.read_bytes() for p in Path(ctx.settings.data_dir).glob("newton.db*"))
    assert SENTINEL.encode() not in stored  # ...Newton never keeps them


def test_a_stream_that_ends_early_says_so(local: TestClient) -> None:
    ready(local, fake={"stream_cut_after": 2})
    with local.stream("POST", "/v1/chat/completions", json={
        "model": "fake/echo", "stream": True,
        "messages": [{"role": "user", "content": "a b c d"}]}) as r:  # fmt: skip
        events = [line for line in r.iter_lines() if line.startswith("data: ")]
    assert "data: [DONE]" not in events
    assert json.loads(events[-1][6:])["error"]["code"] == "upstream_disconnected"
    for line in events[:-1]:
        json.loads(line[6:])
    assert local.get("/router/requests?limit=1").json()[0]["error"] == "upstream_disconnected"


def test_sse_framing_survives_split_crlf() -> None:
    """Driven directly: chunks split between \\r and \\n, and a last event without
    its blank line."""
    from newton_agentd.serving import router as router_mod

    async def run() -> list[bytes]:
        chunks = [b'data: {"a":1}\r\n\r', b'\ndata: {"b":2}\r\n\r\n', b"data: [DONE]"]

        async def body() -> Any:
            for c in chunks:
                yield c

        upstream = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=body())  # fmt: skip
        fake_router = router_mod.Router.__new__(router_mod.Router)
        fake_router._flights = {}
        fake_router._queue = []
        fake_router.dispatch = lambda: None  # type: ignore[method-assign]
        fake_router._log = lambda record: None  # type: ignore[method-assign]
        flight = router_mod.Flight(router_mod.Endpoint("s", "h", "fake", "m", None, "", 1, None))
        relay = fake_router._relay(flight, upstream, {}, {}, time.monotonic())
        out = [chunk async for chunk in relay.body_iterator]  # type: ignore[attr-defined]
        return out

    out = asyncio.run(run())
    assert out == [b'data: {"a":1}\n\n', b'data: {"b":2}\n\n', b"data: [DONE]\n\n"]


def test_least_loaded_first_and_no_head_of_line_blocking(local: TestClient) -> None:
    a1 = ready(local, "a1", parallel=4, fake={"reply_delay_s": 1})
    a2 = ready(local, "a2", parallel=4, fake={"reply_delay_s": 1})
    with ThreadPoolExecutor(4) as pool:
        assert all(r.status_code == 200 for r in pool.map(lambda i: ask(local), range(4)))
    assert engine_stats(a1)["max_active"] == 2 and engine_stats(a2)["max_active"] == 2
    other = ready(local, "other", model="fake/other", parallel=1)
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    local.app.state.ctx.settings.router_queue_timeout = 5  # type: ignore[attr-defined]
    for svc in (a1, a2):  # fill fake/echo: 8 busy, then one more waiting
        local.post(f"/services/{svc['id']}/drain?seconds=60")  # not routable any more
    with ThreadPoolExecutor(2) as pool:
        stuck = pool.submit(ask, local)  # waits: nothing serves fake/echo now
        wait_for(router.waiting, lambda n: n == 1, timeout=5)
        t0 = time.time()
        assert ask(local, "fake/other").status_code == 200  # not behind it
        assert time.time() - t0 < 3
        assert engine_stats(other)["count"] == 1
        assert stuck.result().json()["error"]["code"] == "queue_timeout"


def test_waiters_are_served_in_arrival_order(local: TestClient) -> None:
    ready(local, parallel=1, fake={"reply_delay_s": 0.8})
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    with ThreadPoolExecutor(3) as pool:
        first = pool.submit(ask, local, text="1")
        wait_for(router.in_flight, lambda n: n == 1, timeout=5)
        second = pool.submit(ask, local, text="2")
        wait_for(router.waiting, lambda n: n == 1, timeout=5)
        third = pool.submit(ask, local, text="3")
        wait_for(router.waiting, lambda n: n == 2, timeout=5)
        ids = [f.result().json()["id"] for f in (first, second, third)]
    assert ids == ["fake-1", "fake-2", "fake-3"]  # the engine saw them in order


def test_a_release_wakes_the_queue_by_itself(local: TestClient) -> None:
    ready(local, parallel=1, fake={"reply_delay_s": 0.8})
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    ctx.settings.router_queue_timeout = 3

    async def no_safety_net() -> None:
        ctx.router._task.cancel()

    local.portal.call(no_safety_net)  # type: ignore[attr-defined]
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(ask, local)
        wait_for(ctx.router.in_flight, lambda n: n == 1, timeout=5)
        second = pool.submit(ask, local)
        assert first.result().status_code == 200
        assert second.result().status_code == 200


def test_a_leased_host_gets_nothing_while_others_serve(local: TestClient) -> None:
    """Two hosts serving one model: the leased one is skipped, not the model."""
    here = ready(local, "here")
    there = ready(local, "there")
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    real_endpoints, real_known = router.endpoints, router._known

    def moved(rows: list[Any]) -> list[Any]:
        for r in rows:  # pretend `there` runs on another host, "spark"
            if (r.service_id if hasattr(r, "service_id") else r["id"]) == there["id"]:
                if hasattr(r, "host_id"):
                    r.host_id = "spark"
                else:
                    r["host_id"] = "spark"
        return rows

    router.endpoints = lambda: moved(real_endpoints())
    router._known = lambda: moved(real_known())
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    ctx.scheduler.retain_leases = lambda: None  # no real job holds this lease
    # Ties go to the oldest: make it the leased one, so only the lease keeps it out.
    ctx.db.execute("UPDATE services SET created_at = 0 WHERE id = ?", (there["id"],))
    router._leases["spark"] = _lease("spark")
    for _ in range(6):
        r = ask(local)
        assert r.status_code == 200 and r.headers["X-Newton-Service"] == here["id"]
    paused = {s["id"]: s for m in router.models() for s in m["newton"]["services"]}
    assert paused[there["id"]]["paused"] is True and paused[here["id"]]["paused"] is False


def _lease(host_id: str) -> Any:
    from newton_agentd.serving.router import Lease

    return Lease(host_id, "job-elsewhere")


@pytest.fixture
def some_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the selftest sleeping 2.25 s counts as timed."""
    monkeypatch.setattr(scheduler_mod, "_alone", lambda manifest: '"2.25"' in manifest)


@pytest.mark.usefixtures("some_alone")
def test_nothing_starts_ahead_of_a_timed_job_clearing_its_host(local: TestClient) -> None:
    ready(local, fake={"reply_delay_s": 3})
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    with ThreadPoolExecutor(1) as pool:
        busy = pool.submit(ask, local)
        wait_for(router.in_flight, lambda n: n == 1, timeout=5)
        timed = selftest(local, sleep=2.25)
        wait_for(lambda: router.lease_holder("local"), lambda h: h == timed, timeout=10)
        plain = selftest(local, sleep=0.5)
        time.sleep(1.5)
        assert local.get(f"/jobs/{plain}").json()["state"] == "queued"  # behind the timed one
        busy.result()
    done_timed = wait_job(local, timed, {"succeeded"}, timeout=30)
    done_plain = wait_job(local, plain, {"succeeded"}, timeout=30)
    assert done_plain["started_at"] >= done_timed["started_at"]


@pytest.mark.usefixtures("alone")
def test_a_loading_model_holds_the_timed_job_and_is_a_caveat(local: TestClient) -> None:
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    ctx.settings.lease_drain_timeout = 3
    r = local.post("/services", json={"host_id": "local", "name": "slow", "settings": {
        **FAKE, "fake": {"startup_delay_s": 30}}})  # fmt: skip
    wait_state(local, r.json()["id"], "starting")
    job = selftest(local, sleep=0.5)
    note = wait_for(lambda: local.get(f"/jobs/{job}").json()["error"] or "",
                    lambda e: "to finish loading" in e, timeout=10)  # fmt: skip
    assert r.json()["id"] in note
    wait_job(local, job, {"succeeded"}, timeout=30)
    lease = next(e["data"] for e in local.get(f"/events?entity_id={job}").json()
                 if e["kind"] == "device_lease")  # fmt: skip
    assert lease["loading_at_start"] == [r.json()["id"]]
    assert any("still loading" in c for c in lease["caveats"])


@pytest.mark.usefixtures("alone")
def test_the_lease_is_given_back_while_collecting(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    gate = threading.Event()
    real = ctx.scheduler._collect_one

    async def slow_collect(job: Any) -> None:
        while not gate.is_set():
            await asyncio.sleep(0.1)
        await real(job)

    monkeypatch.setattr(ctx.scheduler, "_collect_one", slow_collect)
    job = selftest(local, sleep=0.2)
    wait_job(local, job, {"collecting"}, timeout=20)
    wait_for(lambda: ctx.router.lease_holder("local"), lambda h: h is None, timeout=5)
    gate.set()
    wait_job(local, job, {"succeeded"}, timeout=20)


@pytest.mark.usefixtures("alone")
def test_a_transient_submit_error_keeps_the_job_and_its_host(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newton_agentd.runners.base import RunnerError

    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    calls: list[str] = []
    runner_holder: dict[str, Any] = {}

    async def grab() -> None:
        runner_holder["r"] = await ctx.hosts.runner("local")

    local.portal.call(grab)  # type: ignore[attr-defined]
    runner = runner_holder["r"]
    real_submit = runner.submit

    async def flaky(manifest: dict[str, Any], bundle: bytes) -> Any:
        calls.append(manifest["job_id"])
        if len(calls) == 1:
            raise RunnerError("blip", code="unreachable", transient=True)
        return await real_submit(manifest, bundle)

    monkeypatch.setattr(runner, "submit", flaky)
    job = selftest(local, sleep=0.2)
    held = wait_for(lambda: local.get(f"/jobs/{job}").json(),
                    lambda j: j["error"] == "blip", timeout=10)  # fmt: skip
    assert held["state"] == "submitting" and ctx.router.lease_holder("local") == job
    done = wait_job(local, job, {"succeeded"}, timeout=30)
    assert len(calls) == 2 and calls[0] == calls[1]  # the same remote id, retried
    assert done["attempt"] == 1


def test_leaving_the_queue_is_seen_at_once(live: Live) -> None:
    ready(live.http, parallel=1, fake={"reply_delay_s": 4})
    router = live.ctx.router
    holder = threading.Thread(target=ask, args=(live.http,))
    holder.start()
    wait_for(router.in_flight, lambda n: n == 1, timeout=5)
    with pytest.raises(httpx.ReadTimeout):
        ask(httpx.Client(base_url=live.base, headers=AUTH, timeout=0.5, trust_env=False))
    wait_for(router.waiting, lambda n: n == 0, timeout=1)
    assert router.in_flight() == 1
    holder.join()


def test_a_dropped_stream_frees_its_slot_and_closes_upstream_at_once(live: Live) -> None:
    ready(live.http, parallel=1, fake={"reply_delay_s": 30})
    router = live.ctx.router
    with httpx.Client(base_url=live.base, headers=AUTH, timeout=30, trust_env=False) as c, \
            c.stream("POST", "/v1/chat/completions", json={
                "model": "fake/echo", "stream": True,
                "messages": [{"role": "user", "content": "a b c d e f"}]}) as r:  # fmt: skip
        next(r.iter_lines())
        flight = next(f for fs in router._flights.values() for f in fs)
    t0 = time.time()
    wait_for(router.in_flight, lambda n: n == 0, timeout=1.5)
    wait_for(flight.closed.is_set, bool, timeout=1.5)
    assert time.time() - t0 < 1.5


@pytest.mark.usefixtures("alone")
def test_a_client_that_stops_reading_cannot_hold_a_timed_job(live: Live) -> None:
    ready(live.http, fake={"reply_delay_s": 60})
    live.ctx.settings.lease_drain_timeout = 1.0
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", int(live.base.rsplit(":", 1)[1])))
    body = json.dumps({"model": "fake/echo", "stream": True, "messages": [
        {"role": "user", "content": " ".join(["word"] * 300000)}]}).encode()  # fmt: skip
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
                 b"Content-Type: application/json\r\nContent-Length: "
                 + str(len(body)).encode() + b"\r\n\r\n" + body)  # fmt: skip
    sock.recv(100)  # then never again
    try:
        wait_for(live.ctx.router.in_flight, lambda n: n == 1, timeout=10)
        job = selftest(live.http, sleep=0.5)
        wait_job(live.http, job, {"succeeded"}, timeout=30)  # type: ignore[arg-type]
    finally:
        sock.close()


def test_shutdown_ends_queued_and_running_requests_with_a_reason(local: TestClient) -> None:
    ready(local, parallel=1, fake={"reply_delay_s": 5})
    router = local.app.state.ctx.router  # type: ignore[attr-defined]
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(ask, local)
        wait_for(router.in_flight, lambda n: n == 1, timeout=5)
        queued = pool.submit(ask, local)
        wait_for(router.waiting, lambda n: n == 1, timeout=5)
        local.portal.call(_begin_shutdown, router)  # type: ignore[attr-defined]
        for f in (running, queued):
            r = f.result(timeout=10)
            assert r.status_code == 503 and r.json()["error"]["code"] == "shutdown"
    assert ask(local).json()["error"]["code"] == "shutdown"


async def _begin_shutdown(router: Any) -> None:
    router.begin_shutdown()


def test_models_without_latest_and_log_retention(local: TestClient) -> None:
    ctx = local.app.state.ctx  # type: ignore[attr-defined]
    router = ctx.router
    router._known = lambda: [{"id": "svc-aaaaaaaa", "host_id": "local", "state": "ready",
                              "engine": "ollama", "model": "qwen2.5:latest",
                              "revision": "abc123def456", "parallel": 1}]  # fmt: skip
    for name in ("qwen2.5", "qwen2.5:latest", "qwen2.5@abc1"):
        assert router.resolve(name)[1] == "model:qwen2.5:latest@abc123def456", name
    ctx.settings.router_log_rows = 3
    for i in range(5):
        router._log({"id": f"req-{i}", "path": "/v1/x", "created_at": time.time() + i,
                     "attempts": 0, "stream": 0})  # fmt: skip
    router.prune_log()
    assert [r["id"] for r in router.requests(None, 10)] == ["req-4", "req-3", "req-2"]
