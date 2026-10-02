"""SV1: model services next to jobs, against the fake OpenAI-compatible engine.

Lifecycle (start, ready, answer, stop, drain), failures (crash, timeout, bad
health, an engine that ignores SIGTERM, a supervisor that dies), memory admission,
spec validation, the engines' fixed argv, and the HTTP API.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from newton_worker import procs
from newton_worker import services as svc
from newton_worker.jobs import JobError, pid_alive
from newton_worker.run_service import Supervisor
from newton_worker.server import serve
from test_worker import TOKEN, Client


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[svc.ServiceStore]:
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    monkeypatch.setenv("NEWTON_SERVICE_STOP_GRACE", "1")
    s = svc.ServiceStore(tmp_path / "home")
    create = s.create

    def create_and_remember(raw: Any, api_key: Any = None) -> dict[str, Any]:
        out = create(raw, api_key=api_key)
        if out.get("api_key") or api_key:
            KEYS[out["service_id"]] = out.get("api_key") or api_key
        return out

    s.create = create_and_remember  # type: ignore[method-assign]
    yield s
    for status in s.list():
        if status["state"] not in svc.TERMINAL:
            s.stop(status["service_id"])
    for status in s.list():
        procs.stop_group(status.get("engine_pid"), status.get("engine_identity"), grace=1)
        if isinstance(status.get("supervisor_pid"), int) and pid_alive(status["supervisor_pid"]):
            os.kill(status["supervisor_pid"], signal.SIGKILL)


def fake(service_id: str = "echo", memory_gb: float = 0.5, **settings: Any) -> dict[str, Any]:
    return {"service_id": service_id, "engine": "fake", "model": "fake/echo",
            "memory_gb": memory_gb, "startup_timeout_s": 30, "fake": settings or None}  # fmt: skip


def wait_state(store: svc.ServiceStore, sid: str, states: tuple[str, ...], timeout: float = 30
               ) -> dict[str, Any]:  # fmt: skip
    deadline = time.time() + timeout
    status = store.status(sid)
    while status["state"] not in states:
        if time.time() > deadline:
            raise AssertionError(f"{sid} never reached {states}: {status}")
        time.sleep(0.1)
        status = store.status(sid)
    return status


KEYS: dict[str, str] = {}  # service_id -> the API key create() returned once


def chat(port: int, text: str, service_id: str = "echo") -> dict[str, Any]:
    body = json.dumps({"model": "fake/echo", "messages": [{"role": "user", "content": text}]})
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body.encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {KEYS.get(service_id, '')}"},
    )  # fmt: skip
    with urllib.request.urlopen(req, timeout=10) as resp:
        out: dict[str, Any] = json.load(resp)
        return out


@pytest.fixture
def plenty_of_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 100 * svc.GIB})  # fmt: skip


# -- lifecycle ------------------------------------------------------------------------


@pytest.mark.usefixtures("plenty_of_memory")
def test_start_answer_stop(store: svc.ServiceStore) -> None:
    started = store.create(fake(startup_delay_s=1.0))
    assert started["state"] == "starting" and started["port"] > 0
    loading = wait_state(store, "echo", ("loading", "ready"))
    ready = wait_state(store, "echo", ("ready",))
    assert ready["healthy"] is True and ready["ready_at"] >= loading["created_at"]
    reply = chat(ready["port"], "hello")
    assert reply["choices"][0]["message"]["content"] == "echo: hello"
    engine = ready["engine_pid"]

    store.stop("echo")
    stopped = wait_state(store, "echo", ("stopped",))
    assert stopped["error"] is None and not pid_alive(engine)
    with pytest.raises(urllib.error.URLError):
        chat(ready["port"], "still there?")
    # Same spec again: a fresh start, on a fresh port.
    again = store.create(fake(startup_delay_s=1.0))
    assert again["state"] == "starting"
    assert wait_state(store, "echo", ("ready",))["healthy"] is True


@pytest.mark.usefixtures("plenty_of_memory")
def test_drain_then_stop(store: svc.ServiceStore) -> None:
    wait_state(store, store.create(fake())["service_id"], ("ready",))
    draining = store.drain("echo", 5.0)
    assert draining["state"] == "draining"  # reported at once: the router stops sending
    assert chat(draining["port"], "in flight")["choices"]  # still answers while draining
    assert wait_state(store, "echo", ("stopped",), timeout=15)["error"] is None
    # Started again with the same spec, an old drain request must not stop it.
    store.create(fake())
    wait_state(store, "echo", ("ready",))
    time.sleep(1.5)
    assert store.status("echo")["state"] == "ready"


@pytest.mark.usefixtures("plenty_of_memory")
def test_crash_is_reported_with_the_engines_last_words(store: svc.ServiceStore) -> None:
    store.create(fake(crash_after_s=1.0))
    failed = wait_state(store, "echo", ("failed",))
    assert "the engine exited (exit code 3)" in failed["error"]
    assert "fake model server for fake/echo" in failed["error"]  # log tail


@pytest.mark.usefixtures("plenty_of_memory")
def test_slow_start_times_out(store: svc.ServiceStore) -> None:
    spec = fake(startup_delay_s=60)
    spec["startup_timeout_s"] = 5
    store.create(spec)
    failed = wait_state(store, "echo", ("failed",), timeout=20)
    assert "did not start in time" in failed["error"]
    assert not pid_alive(failed["engine_pid"])


@pytest.mark.usefixtures("plenty_of_memory")
def test_stop_while_loading_is_prompt(store: svc.ServiceStore) -> None:
    store.create(fake(startup_delay_s=60))
    wait_state(store, "echo", ("starting",))
    time.sleep(1)
    t0 = time.time()
    store.stop("echo")
    stopped = wait_state(store, "echo", ("stopped",), timeout=10)
    assert time.time() - t0 < 8 and not pid_alive(stopped["engine_pid"])


@pytest.mark.usefixtures("plenty_of_memory")
def test_health_turning_bad_is_noticed(store: svc.ServiceStore) -> None:
    store.create(fake(unhealthy_after_s=1.5))
    wait_state(store, "echo", ("ready",))
    deadline = time.time() + 15
    while store.status("echo")["healthy"] is not False:
        assert time.time() < deadline, store.status("echo")
        time.sleep(0.2)
    assert store.status("echo")["state"] == "ready"  # unhealthy, still running: SV3 decides


@pytest.mark.usefixtures("plenty_of_memory")
def test_an_engine_ignoring_sigterm_is_still_stopped(store: svc.ServiceStore) -> None:
    ready = wait_state(store, store.create(fake(ignore_sigterm=True))["service_id"], ("ready",))
    store.stop("echo")
    stopped = wait_state(store, "echo", ("stopped",), timeout=15)
    deadline = time.time() + 5
    while pid_alive(ready["engine_pid"]) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(stopped["engine_pid"])  # SIGKILL after the grace period


@pytest.mark.usefixtures("plenty_of_memory")
def test_a_dead_supervisor_is_lost_and_its_engine_stopped(store: svc.ServiceStore) -> None:
    ready = wait_state(store, store.create(fake())["service_id"], ("ready",))
    os.kill(ready["supervisor_pid"], signal.SIGKILL)  # e.g. OOM killer
    lost = wait_state(store, "echo", ("lost",))
    assert "supervisor is gone" in lost["error"]
    deadline = time.time() + 5
    while pid_alive(ready["engine_pid"]) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(ready["engine_pid"])  # no orphan holding GPU memory


@pytest.mark.usefixtures("plenty_of_memory")
def test_services_survive_a_worker_restart(store: svc.ServiceStore) -> None:
    ready = wait_state(store, store.create(fake())["service_id"], ("ready",))
    restarted = svc.ServiceStore(store.root)  # a new worker process on the same root
    assert [s["service_id"] for s in restarted.list()] == ["echo"]
    assert restarted.status("echo")["state"] == "ready"
    assert chat(ready["port"], "still here")["choices"]


@pytest.mark.usefixtures("plenty_of_memory")
def test_same_id_is_idempotent_but_not_redefinable(store: svc.ServiceStore) -> None:
    first = store.create(fake())
    assert store.create(fake())["port"] == first["port"]  # retried request: same service
    with pytest.raises(JobError) as e:
        store.create(fake(memory_gb=2.0))  # while it runs, the id is taken
    assert e.value.status == 409
    store.stop("echo")
    wait_state(store, "echo", ("stopped",))
    assert store.create(fake(memory_gb=2.0))["memory_gb"] == 2.0  # stopped: reusable


# -- memory admission ----------------------------------------------------------------------


def test_admission_keeps_a_reserve_and_counts_loading_services(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 30 * svc.GIB})  # fmt: skip
    # Reserve: max(8 GB, 10% of 128 GB) = 12.8 GB. 30 - 12.8 = 17.2 GB to give out.
    with pytest.raises(JobError) as e:
        store.create(fake("big", memory_gb=20))
    assert e.value.status == 409 and "kept free for the system" in e.value.message
    store.create(fake("first", memory_gb=10, startup_delay_s=60))  # loading: memory not taken yet
    with pytest.raises(JobError) as e:
        store.create(fake("second", memory_gb=10))  # 30 - 10 (promised) - 10 < 12.8
    assert "10.0 GB promised to services still loading" in e.value.message
    assert store.create(fake("small", memory_gb=5))["state"] == "starting"
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "1")
    assert store.admission(svc.validate_spec(fake("x", memory_gb=10)))["ok"] is True


def test_memory_is_read_from_this_host() -> None:
    mem = svc.memory_now()
    assert mem["total"] and mem["total"] > 1 * svc.GIB
    assert mem["available"] is not None and 0 < mem["available"] <= mem["total"]


# -- spec validation and engine commands ------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"args": "--trust-remote-code"}, "unknown service fields"),
        ({"engine": "llama.cpp"}, "engine must be one of"),
        ({"engine": "ollama", "model": "llama3.2:3b"}, "pinned revision"),
        ({"engine": "ollama", "model": "x; rm -rf ~", "revision": "a" * 12}, "invalid model"),
        ({"engine": "vllm", "model": "org/model", "revision": "main"}, "40-hex commit"),
        ({"trust_remote_code": True}, "only applies to vllm"),
        ({"engine": "ollama", "model": "m:1", "revision": "a" * 12, "fake": {}},
         "only apply to the fake engine"),
        ({"memory_gb": 0}, "memory_gb"),
        ({"parallel": 1000}, "parallel"),
        ({"service_id": "../escape"}, "invalid service_id"),
        ({"service_id": "abc\n"}, "invalid service_id"),  # "$" would accept a final newline
        ({"engine": "vllm", "model": "--trust-remote-code/x", "revision": "c" * 40},
         "invalid model"),
        ({"fake": {"hold_memory_mb": 2048}}, "more memory than memory_gb"),
    ],
)  # fmt: skip
def test_specs_are_typed_with_no_free_form_flags(change: dict[str, Any], message: str) -> None:
    spec = {**fake(), "fake": None, **change}
    with pytest.raises(JobError, match=message):
        svc.validate_spec(spec)


def test_engine_commands_are_fixed_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "engine_executable", lambda engine: f"/opt/bin/{engine}")
    ollama = svc.validate_spec({"service_id": "o", "engine": "ollama", "model": "gpt-oss:120b",
                                "revision": "a951a23b46a1", "memory_gb": 70, "parallel": 8,
                                "context_length": 16384})  # fmt: skip
    argv, env = svc.engine_command(ollama, 4321, tmp_path, 128 * svc.GIB)
    assert argv == ["/opt/bin/ollama", "serve"]
    assert env == {
        "OLLAMA_HOST": "127.0.0.1:4321",
        "OLLAMA_MODELS": str(tmp_path / "models" / "ollama"),  # its own store
        "OLLAMA_NUM_PARALLEL": "8",
        "OLLAMA_CONTEXT_LENGTH": "16384",
        "OLLAMA_KV_CACHE_TYPE": "q8_0",
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_KEEP_ALIVE": "-1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
    }
    vllm = svc.validate_spec({"service_id": "v", "engine": "vllm", "model": "org/model",
                              "revision": "c" * 40, "memory_gb": 40, "kv_cache_type": "q8_0",
                              "trust_remote_code": True})  # fmt: skip
    argv, env = svc.engine_command(vllm, 9000, tmp_path, 128 * svc.GIB)
    assert argv[:3] == ["/opt/bin/vllm", "serve", "org/model"]
    # vLLM takes this share of *all* memory up front: sized from what was declared.
    assert argv[argv.index("--gpu-memory-utilization") + 1] == f"{40 / 128:.3f}"
    assert env == {"HF_HUB_CACHE": str(tmp_path / "models" / "vllm")}  # keeps the HF login
    assert argv[argv.index("--revision") + 1] == "c" * 40
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--kv-cache-dtype") + 1] == "fp8" and argv[-1] == "--trust-remote-code"


def test_a_missing_engine_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "engine_executable", lambda engine: None)
    spec = svc.validate_spec({"service_id": "v", "engine": "vllm", "model": "org/model",
                              "revision": "c" * 40, "memory_gb": 40})  # fmt: skip
    with pytest.raises(JobError) as e:
        svc.engine_command(spec, 9000, tmp_path, 128 * svc.GIB)
    assert e.value.status == 422 and "not installed" in e.value.message


# -- ollama preparation (pull, pinned digest, resident) -----------------------------------------


class FakeOllamaAPI:
    def __init__(self, digest: str) -> None:
        self.calls: list[str] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def reply(self, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                api.calls.append(f"GET {self.path}")
                self.reply({"models": [{"name": "llama3.2:3b", "digest": digest}]})

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                api.calls.append(f"POST {self.path}")
                self.reply({"status": "success"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]


def ollama_supervisor(tmp_path: Path, port: int, revision: str) -> Supervisor:
    d = tmp_path / "svc"
    d.mkdir()
    spec = svc.validate_spec({"service_id": "o", "engine": "ollama", "model": "llama3.2:3b",
                              "revision": revision, "memory_gb": 3})  # fmt: skip
    (d / "spec.json").write_text(json.dumps(spec))
    (d / "status.json").write_text(json.dumps({"state": "starting", "port": port}))
    return Supervisor(d)


def test_ollama_model_is_pulled_checked_and_made_resident(tmp_path: Path) -> None:
    api = FakeOllamaAPI(digest="a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72")
    sup = ollama_supervisor(tmp_path, api.port, "a80c4f17acd5")
    sup.prepare(time.time() + 30)
    assert api.calls == ["POST /api/pull", "GET /api/tags", "POST /api/generate"]
    api.server.shutdown()


def test_a_moved_ollama_tag_is_refused(tmp_path: Path) -> None:
    api = FakeOllamaAPI(digest="ffffffffffff" + "0" * 52)  # the tag now points elsewhere
    sup = ollama_supervisor(tmp_path, api.port, "a80c4f17acd5")
    with pytest.raises(RuntimeError, match="not the pinned a80c4f17acd5"):
        sup.prepare(time.time() + 30)
    assert "POST /api/generate" not in api.calls  # never loaded
    api.server.shutdown()


# -- the HTTP API ----------------------------------------------------------------------------------


@pytest.mark.usefixtures("plenty_of_memory")
def test_services_over_the_worker_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    server = serve(tmp_path / "home", TOKEN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()  # fmt: skip
    client = Client(server.server_address[1])
    try:
        assert client.call("POST", "/services/admission", fake())["ok"] is True
        created = client.call("POST", "/services", fake())
        assert created["state"] == "starting"
        deadline = time.time() + 30
        while client.call("GET", "/services/echo")["state"] != "ready":
            assert time.time() < deadline
            time.sleep(0.1)
        assert [s["service_id"] for s in client.call("GET", "/services")] == ["echo"]
        logs = client.call("GET", "/services/echo/logs?offset=0")
        assert "fake model server" in logs["data"]
        with pytest.raises(urllib.error.HTTPError) as e:
            client.call("POST", "/services", {**fake(), "args": "--anything"})
        assert e.value.code == 400
        assert client.call("POST", "/services/echo/stop")["state"] in ("stopping", "stopped")
        deadline = time.time() + 10  # well under the 20 s grace: SIGTERM itself must work
        while client.call("GET", "/services/echo")["state"] != "stopped":
            assert time.time() < deadline
            time.sleep(0.1)
    finally:
        for s in server.services.list():
            if s["state"] not in svc.TERMINAL:
                server.services.stop(s["service_id"])
        server.shutdown()
        server.server_close()


# -- review round: process identity, orphans, races --------------------------------------------


def _group(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, start_new_session=True)


def stale_status(store: svc.ServiceStore, sid: str, **fields: Any) -> Path:
    d = store.service_dir(sid)
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps(svc.validate_spec(fake(sid))))
    status = {"service_id": sid, "state": "ready", "port": 1, "memory_gb": 0.5,
              "created_at": time.time() - 3600, "ready_at": time.time() - 3600,
              "supervisor_pid": 999999, **fields}  # fmt: skip
    (d / "status.json").write_text(json.dumps(status))
    return d


def test_a_reused_pid_is_never_killed(store: svc.ServiceStore) -> None:
    # After a reboot the old engine pid may belong to anyone: here, an unrelated
    # process group of the same user. Neither status nor admission may touch it.
    bystander = _group(["sleep", "60"])
    try:
        for name, recorded in (
            ("other-start", {"boot_id": procs.boot_id(), "start": "1"}),
            ("other-boot", {"boot_id": "an-earlier-boot", "start": None}),
            ("unknown", None),
        ):
            stale_status(store, name, engine_pid=bystander.pid, engine_identity=recorded)
            assert store.status(name)["state"] == "lost"
            assert bystander.poll() is None, name
    finally:
        bystander.kill()
        bystander.wait()


def test_the_identity_check_recognizes_our_own_group() -> None:
    group = _group(["sleep", "60"])
    try:
        recorded = procs.identity(group.pid)
        assert procs.ours(group.pid, recorded) is True
        assert procs.stop_group(group.pid, recorded, grace=1) is True
        group.wait(timeout=5)
    finally:
        if group.poll() is None:
            group.kill()


@pytest.mark.usefixtures("plenty_of_memory")
def test_engine_children_die_with_the_engine(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ollama serve forks a runner; vLLM forks its workers. If the engine's main
    # process dies first, the children (holding the model's memory) must go too.
    marker = tmp_path / "child.pid"
    real = svc.engine_command

    def engine_with_child(spec: dict[str, Any], port: int, root: Path, total: Any = None
                          ) -> tuple[list[str], dict[str, str]]:  # fmt: skip
        argv, env = real(spec, port, root, total)
        shell = f"{' '.join(argv)} & echo $! > {marker}; sleep 3; exit 7"
        return ["/bin/sh", "-c", shell], env

    monkeypatch.setattr(svc, "engine_command", engine_with_child)
    store.create(fake())
    failed = wait_state(store, "echo", ("failed",))
    assert "exit code 7" in failed["error"]
    child = int(marker.read_text())
    deadline = time.time() + 10
    while pid_alive(child) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(child)


@pytest.mark.usefixtures("plenty_of_memory")
def test_a_crash_while_starting_fails_fast(store: svc.ServiceStore) -> None:
    store.create(fake(startup_delay_s=60, crash_after_s=1.0))  # dies long before "up"
    t0 = time.time()
    failed = wait_state(store, "echo", ("failed",), timeout=20)
    assert "exited while starting" in failed["error"] and time.time() - t0 < 10


@pytest.mark.usefixtures("plenty_of_memory")
def test_stop_with_a_dead_supervisor_stops_the_engine(store: svc.ServiceStore) -> None:
    ready = wait_state(store, store.create(fake())["service_id"], ("ready",))
    os.kill(ready["supervisor_pid"], signal.SIGKILL)
    stopped = store.stop("echo")
    assert stopped["state"] == "stopped"
    deadline = time.time() + 10
    while pid_alive(ready["engine_pid"]) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(ready["engine_pid"])


@pytest.mark.usefixtures("plenty_of_memory")
def test_stop_right_after_create_is_stopped_not_lost(store: svc.ServiceStore) -> None:
    store.create(fake())
    store.stop("echo")  # the supervisor may not even have its handler yet
    final = wait_state(store, "echo", ("stopped", "lost"), timeout=20)
    assert final["state"] == "stopped", final


@pytest.mark.usefixtures("plenty_of_memory")
def test_stopping_shows_while_a_stubborn_engine_gets_its_grace(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEWTON_SERVICE_STOP_GRACE", "3")
    wait_state(store, store.create(fake(ignore_sigterm=True))["service_id"], ("ready",))
    store.stop("echo")
    time.sleep(1)
    raw = json.loads((store.service_dir("echo") / "status.json").read_text())
    assert raw["state"] == "stopping"  # the supervisor itself says so, not only the overlay
    assert wait_state(store, "echo", ("stopped",), timeout=15)["state"] == "stopped"


@pytest.mark.usefixtures("plenty_of_memory")
def test_a_ps_hiccup_does_not_kill_a_healthy_service(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = wait_state(store, store.create(fake())["service_id"], ("ready",))
    monkeypatch.setattr(svc, "last_argument_is", lambda pid, path: None)  # can't tell
    assert store.status("echo")["state"] == "ready"
    assert pid_alive(ready["engine_pid"])


def test_a_supervisor_is_matched_exactly(store: svc.ServiceStore, tmp_path: Path) -> None:
    longer = store.service_dir("chat-2")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                             "newton_worker.run_service", str(longer)])  # fmt: skip
    try:
        assert svc.supervisor_alive(proc.pid, longer) is True
        assert svc.supervisor_alive(proc.pid, store.service_dir("chat")) is False  # a prefix
    finally:
        proc.kill()
        proc.wait()


# -- review round: admission ---------------------------------------------------------------------


def _dummy_supervisor(d: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             "newton_worker.run_service", str(d)])  # fmt: skip


def test_loading_and_stopping_services_count_until_their_engine_is_gone(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 100 * svc.GIB})  # fmt: skip
    supervisors = []
    try:
        for name, state, ready_at in (("pulling", "loading", None),
                                      ("aborting", "stopping", None),
                                      ("leaving", "stopping", 1.0)):  # fmt: skip
            d = stale_status(store, name, state=state, memory_gb=10.0, ready_at=ready_at)
            proc = _dummy_supervisor(d)
            supervisors.append(proc)
            status = json.loads((d / "status.json").read_text())
            status["supervisor_pid"] = proc.pid
            (d / "status.json").write_text(json.dumps(status))
        # loading: not taken yet; stopping before ready: may still be allocating;
        # stopping after ready: already in MemAvailable.
        assert store.admission(svc.validate_spec(fake("x")))["pending_gb"] == 20.0
    finally:
        for proc in supervisors:
            proc.kill()
            proc.wait()


def test_unknown_memory_refuses(store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": None, "available": None})
    with pytest.raises(JobError, match="can't read this host's available memory"):
        store.create(fake())


def test_downloads_need_disk(store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 100 * svc.GIB})  # fmt: skip
    monkeypatch.setattr(svc.shutil, "disk_usage",
                        lambda path: type("U", (), {"free": 50 * svc.GIB})())  # fmt: skip
    spec = {"service_id": "big", "engine": "ollama", "model": "gpt-oss:120b",
            "revision": "a951a23b46a1", "memory_gb": 70}  # fmt: skip
    with pytest.raises(JobError, match="not enough disk to download gpt-oss:120b"):
        store.create(spec)
    monkeypatch.setattr(svc, "model_present", lambda spec, root: True)  # already pulled
    assert store.admission(svc.validate_spec(spec)).get("disk") is None


def test_ollama_names_get_their_tag() -> None:
    spec = svc.validate_spec({"service_id": "o", "engine": "ollama", "model": "llama3.2",
                              "revision": "a80c4f17acd5", "memory_gb": 3})  # fmt: skip
    assert spec["model"] == "llama3.2:latest"  # as /api/tags and /api/ps name it


def test_vllm_cannot_take_nearly_all_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                            ) -> None:  # fmt: skip
    monkeypatch.setattr(svc, "engine_executable", lambda engine: "/opt/bin/vllm")
    spec = svc.validate_spec({"service_id": "v", "engine": "vllm", "model": "org/model",
                              "revision": "c" * 40, "memory_gb": 120})  # fmt: skip
    with pytest.raises(JobError, match="more than vLLM may take"):
        svc.engine_command(spec, 9000, tmp_path, 128 * svc.GIB)


def test_gpu_jobs_leave_room_for_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from newton_worker.jobs import JobStore

    root = tmp_path / "home"
    root.mkdir()
    store = JobStore(root)
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 100 * svc.GIB})  # fmt: skip
    monkeypatch.setattr(svc.ServiceStore, "declared_memory", lambda self: 0)
    assert store._cap_beside_services() == "50%"  # no service: the usual cap
    monkeypatch.setattr(svc.ServiceStore, "declared_memory", lambda self: 70 * svc.GIB)
    cap = int(store._cap_beside_services())
    reserve = svc.reserve_bytes(128 * svc.GIB)  # 10% on Linux; a Mac keeps less
    assert cap == 128 * svc.GIB - reserve - 70 * svc.GIB  # beside the services
    monkeypatch.setattr(svc.ServiceStore, "declared_memory", lambda self: 120 * svc.GIB)
    with pytest.raises(JobError, match="not enough memory left for a GPU job"):
        store._cap_beside_services()


# -- review round: a stop interrupts a long model download ---------------------------------------

STUB_OLLAMA = r"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port, pull_seconds, log = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/ps":
            self.reply({"models": [{"name": "llama3.2:latest", "model": "llama3.2:latest"}]})
        else:
            self.reply({"version": "stub", "models": [{"name": "llama3.2:latest",
                        "digest": "a80c4f17acd5" + "0" * 52}]})
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        open(log, "a").write(self.path + " " + body + "\n")
        if self.path == "/api/pull":
            time.sleep(pull_seconds)
        self.reply({"status": "success"})
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _stub_ollama(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pull_seconds: float) -> Path:
    script = tmp_path / "stub_ollama.py"
    script.write_text(STUB_OLLAMA)
    calls = tmp_path / "calls.log"
    monkeypatch.setattr(svc, "model_present", lambda spec, root: True)
    monkeypatch.setattr(
        svc, "engine_command",
        lambda spec, port, root, total=None: (
            [sys.executable, str(script), str(port), str(pull_seconds), str(calls)], {}
        ),
    )  # fmt: skip
    return calls


OLLAMA_SPEC = {"service_id": "o", "engine": "ollama", "model": "llama3.2",
               "revision": "a80c4f17acd5", "memory_gb": 3}  # fmt: skip


@pytest.mark.usefixtures("plenty_of_memory")
def test_ollama_service_end_to_end_with_a_stub_engine(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_ollama(monkeypatch, tmp_path, pull_seconds=0)
    ready = wait_state(store, store.create(OLLAMA_SPEC)["service_id"], ("ready",))
    assert ready["healthy"] is True
    log = calls.read_text().splitlines()
    assert log[0].startswith("/api/pull") and log[-1].startswith("/api/generate")
    assert json.loads(log[-1].split(" ", 1)[1])["keep_alive"] == -1  # resident until stopped


@pytest.mark.usefixtures("plenty_of_memory")
def test_stop_interrupts_a_long_model_download(
    store: svc.ServiceStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_ollama(monkeypatch, tmp_path, pull_seconds=120)
    store.create(OLLAMA_SPEC)
    wait_state(store, "o", ("loading",))  # inside the /api/pull call
    time.sleep(1)
    t0 = time.time()
    store.stop("o")
    stopped = wait_state(store, "o", ("stopped",), timeout=20)
    assert time.time() - t0 < 10 and stopped["error"] is None


@pytest.mark.usefixtures("plenty_of_memory")
def test_each_service_gets_its_own_api_key(store: svc.ServiceStore) -> None:
    started = store.create(fake())
    assert len(started["api_key"]) >= 32
    ready = wait_state(store, "echo", ("ready",))
    assert "api_key" not in ready  # returned once, never in status
    command = json.loads((store.service_dir("echo") / "command.json").read_text())
    assert started["api_key"] not in " ".join(command["argv"])  # not visible in `ps`
    assert chat(ready["port"], "with key")["choices"]
    KEYS["echo"] = "wrong"
    with pytest.raises(urllib.error.HTTPError) as e:
        chat(ready["port"], "without the right key")
    assert e.value.code == 401
    assert "api_key" not in store.create(fake())  # a retried create doesn't hand it out again


# -- SV2 review round: agentd's key, admission that knows the engine ---------------------------


@pytest.mark.usefixtures("plenty_of_memory")
def test_the_key_agentd_sends_is_used_and_never_echoed(store: svc.ServiceStore) -> None:
    key = "k" * 43
    started = store.create(fake("keyed"), api_key=key)
    assert "api_key" not in started  # agentd has it already; nothing to hand back
    ready = wait_state(store, "keyed", ("ready",))
    assert chat(ready["port"], "with agentd's key", "keyed")["choices"]
    assert key not in (store.service_dir("keyed") / "spec.json").read_text()
    assert key not in json.dumps(store.status("keyed"))
    # The retry of a lost answer: same id, same key, nothing new and no key to lose.
    again = store.create(fake("keyed"), api_key=key)
    assert again["port"] == ready["port"] and "api_key" not in again
    with pytest.raises(JobError) as e:
        store.create(fake("other"), api_key="short; rm -rf /")
    assert e.value.status == 400


def test_admission_says_whether_the_engine_could_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 100 * svc.GIB})  # fmt: skip
    store = svc.ServiceStore(tmp_path / "home")
    monkeypatch.setattr(svc, "engine_executable", lambda engine: None)
    vllm = {"service_id": "v", "engine": "vllm", "model": "org/model", "revision": "c" * 40,
            "memory_gb": 8}  # fmt: skip
    assert store.admission(vllm)["engine_installed"] is False
    monkeypatch.setattr(svc, "engine_executable", lambda engine: "/usr/bin/true")
    assert store.admission(vllm)["engine_installed"] is True
    too_big = store.admission({**vllm, "memory_gb": 127})
    assert too_big["ok"] is False and too_big.get("sizing")


def test_the_key_travels_in_a_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 128 * svc.GIB,
                                                    "available": 100 * svc.GIB})  # fmt: skip
    monkeypatch.setenv("NEWTON_SERVICE_HEALTH_EVERY", "0.3")
    server = serve(tmp_path / "home", TOKEN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()  # fmt: skip
    try:
        key = "h" * 43
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/services",
            data=json.dumps(fake("hdr")).encode(), method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                     "X-Newton-Service-Key": key},
        )  # fmt: skip
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert "api_key" not in json.load(resp)
        KEYS["hdr"] = key
        ready = wait_state(server.services, "hdr", ("ready",))
        assert chat(ready["port"], "header key", "hdr")["choices"]
    finally:
        for s in server.services.list():
            if s["state"] not in svc.TERMINAL:
                server.services.stop(s["service_id"])
        server.shutdown()
        server.server_close()
