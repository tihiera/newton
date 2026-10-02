"""B7: an Ollama service starts with no network when its pinned model is already in
the service's store, and a failed pull says what Ollama said."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import pytest
from newton_worker import services as svc
from newton_worker.run_service import Supervisor

PINNED = "a80c4f17acd5"
DIGEST = "a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72"
OFFLINE = (
    'pull model manifest: Get "https://registry.ollama.ai/v2/library/llama3.2/manifests/3b": '
    "dial tcp: lookup registry.ollama.ai: no such host"
)


class OfflineOllama:
    """An Ollama server on a Mac with no network: its local store answers, a pull
    fails the way Ollama reports it (HTTP 500 with {"error": ...})."""

    def __init__(self, digest: Optional[str], pull_status: int = 500,
                 pull_body: Any = None) -> None:  # fmt: skip
        self.calls: list[str] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def reply(self, status: int, body: Any) -> None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                api.calls.append(f"GET {self.path}")
                models = [{"name": "llama3.2:3b", "digest": digest}] if digest else []
                self.reply(200, {"models": models})

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                api.calls.append(f"POST {self.path}")
                if self.path == "/api/pull":
                    body = {"error": OFFLINE} if pull_body is None else pull_body
                    self.reply(pull_status, body)
                else:
                    self.reply(200, {"status": "success"})

            def do_DELETE(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                api.calls.append(f"DELETE {self.path}")
                self.reply(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]


@pytest.fixture
def servers() -> Iterator[list[OfflineOllama]]:
    started: list[OfflineOllama] = []
    yield started
    for api in started:
        api.server.shutdown()


def supervisor(root: Path, api: OfflineOllama, stored: bool) -> Supervisor:
    """A supervisor laid out like the worker's: <root>/services/<id>, with the model's
    manifest in <root>/models/ollama when `stored`."""
    spec = svc.validate_spec({"service_id": "o", "engine": "ollama", "model": "llama3.2:3b",
                              "revision": PINNED, "memory_gb": 3})  # fmt: skip
    d = root / "services" / "o"
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps(spec))
    (d / "status.json").write_text(json.dumps({"state": "starting", "port": api.port}))
    if stored:
        manifest = (root / "models" / "ollama" / "manifests" / "registry.ollama.ai" / "library"
                    / "llama3.2" / "3b")  # fmt: skip
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
    assert svc.model_present(spec, root) is stored
    return Supervisor(d)


def test_a_stored_pinned_model_starts_without_pulling(
    tmp_path: Path, servers: list[OfflineOllama]
) -> None:
    api = OfflineOllama(digest=DIGEST)
    servers.append(api)
    supervisor(tmp_path, api, stored=True).prepare(time.time() + 30)
    # Checked against the pinned digest locally, then loaded: nothing downloaded.
    # No pull: the stored model is loaded, then asked for its trained context.
    assert api.calls == ["GET /api/tags", "POST /api/generate", "POST /api/show"]


def test_a_stored_model_at_another_digest_is_pulled(
    tmp_path: Path, servers: list[OfflineOllama]
) -> None:
    api = OfflineOllama(digest="ffffffffffff" + "0" * 52, pull_status=200,
                        pull_body={"status": "success"})  # fmt: skip
    servers.append(api)
    with pytest.raises(RuntimeError, match=f"not the pinned {PINNED}"):
        supervisor(tmp_path, api, stored=True).prepare(time.time() + 30)
    assert api.calls[:2] == ["GET /api/tags", "POST /api/pull"]
    assert "POST /api/generate" not in api.calls


def test_a_failed_pull_carries_ollamas_own_words(
    tmp_path: Path, servers: list[OfflineOllama]
) -> None:
    api = OfflineOllama(digest=None)
    servers.append(api)
    with pytest.raises(RuntimeError) as e:
        supervisor(tmp_path, api, stored=False).prepare(time.time() + 30)
    message = str(e.value)
    assert message.startswith("ollama couldn't pull llama3.2:3b: ")
    assert "lookup registry.ollama.ai: no such host" in message
    assert "HTTP Error" not in message
    assert api.calls == ["POST /api/pull"]  # nothing else tried


def test_a_pull_error_without_json_still_says_something(
    tmp_path: Path, servers: list[OfflineOllama]
) -> None:
    api = OfflineOllama(digest=None, pull_status=502, pull_body=b"")
    servers.append(api)
    with pytest.raises(RuntimeError, match=r"ollama couldn't pull llama3.2:3b: HTTP 502"):
        supervisor(tmp_path, api, stored=False).prepare(time.time() + 30)


def test_an_error_in_a_successful_answer_is_reported(
    tmp_path: Path, servers: list[OfflineOllama]
) -> None:
    api = OfflineOllama(digest=None, pull_status=200, pull_body={"error": "disk full"})
    servers.append(api)
    with pytest.raises(RuntimeError, match="ollama couldn't pull llama3.2:3b: disk full"):
        supervisor(tmp_path, api, stored=False).prepare(time.time() + 30)


def test_a_pull_reports_its_progress(tmp_path: Path, servers: list[OfflineOllama]) -> None:
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling a", "digest": "sha256:a", "total": 1000, "completed": 250},
        {"status": "pulling b", "digest": "sha256:b", "total": 3000, "completed": 0},
        {"status": "pulling a", "digest": "sha256:a", "total": 1000, "completed": 1000},
        {"status": "success"},
    ]
    body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
    api = OfflineOllama(digest=None, pull_status=200, pull_body=body)
    servers.append(api)
    sup = supervisor(tmp_path, api, stored=False)
    seen: list[object] = []
    update = sup.update

    def record(**fields: object) -> None:
        if "progress" in fields:
            seen.append(fields["progress"])
        update(**fields)

    sup.update = record  # type: ignore[method-assign]
    sup.pull(time.time() + 30)
    assert seen[0] == {"phase": "downloading", "completed": 250, "total": 1000}  # first step
    assert seen[-1] is None  # cleared once the pull is over
    assert json.loads((sup.dir / "status.json").read_text()).get("progress") is None


def test_the_models_trained_context_is_read_from_ollama(
    tmp_path: Path, servers: list[OfflineOllama]
) -> None:
    api = OfflineOllama(digest=DIGEST)
    servers.append(api)
    sup = supervisor(tmp_path, api, stored=True)
    answers = {"/api/show": {"model_info": {"general.architecture": "nemotron",
                                            "nemotron.context_length": 4096}}}  # fmt: skip
    sup.http = lambda method, path, body=None, timeout=5.0: answers.get(path, {})  # type: ignore[method-assign]
    assert sup.trained_context("nemotron-mini:4b") == 4096
    answers["/api/show"] = {"model_info": {}}
    assert sup.trained_context("nemotron-mini:4b") is None
