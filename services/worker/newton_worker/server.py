"""Worker HTTP API (stdlib).

Served on a Unix socket inside the worker home (remote hosts: reached through
OpenSSH stream-local forwarding, so on a shared machine only the owner can even
connect) or on 127.0.0.1 TCP (the local runner). All endpoints also require
`Authorization: Bearer <token>`.

    GET  /health                         → {ok, version, protocol, worker_id}
    GET  /hardware                       → hardware probe
    GET  /jobs                           → [status]
    POST /jobs            (json manifest) → status          (idempotent on job_id)
    PUT  /jobs/{id}/bundle (tar.gz body)  → status
    POST /jobs/{id}/start                 → status          (idempotent)
    GET  /jobs/{id}                       → status
    POST /jobs/{id}/cancel                → status
    GET  /jobs/{id}/logs?stream=&offset=&limit= → log chunk
    GET  /jobs/{id}/artifacts             → tar.gz of manifest.artifact_paths
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import socketserver
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import PROTOCOL_VERSION, __version__, installed, source_digest
from .fsutil import write_json_atomic
from .hardware import probe
from .jobs import JobError, JobStore
from .services import ServiceStore, validate_spec


class Tokens:
    """The bearer tokens this worker accepts: one per Newton install that manages
    it (files in <root>/tokens/, plus a legacy <root>/token), so two Macs or two
    host entries for the same account never lock each other out. A token added
    while the worker runs is picked up on the next request that needs it."""

    def __init__(self, fixed: Optional[str] = None, root: Optional[Path] = None) -> None:
        self.fixed = fixed
        self.root = root
        self._stamp: Optional[tuple[float, ...]] = None
        self._tokens: list[str] = []

    def _files(self) -> list[Path]:
        assert self.root is not None
        files = sorted((self.root / "tokens").glob("*")) if (self.root / "tokens").is_dir() else []
        legacy = self.root / "token"
        return [*files, legacy] if legacy.is_file() else files

    def _reload(self) -> None:
        files = self._files()
        stamp = tuple(f.stat().st_mtime for f in files) + (float(len(files)),)
        if stamp != self._stamp:
            self._tokens = [t for t in (f.read_text().strip() for f in files) if t]
            self._stamp = stamp

    def check(self, presented: str) -> bool:
        candidates = [self.fixed] if self.fixed else []
        if self.root is not None:
            try:
                self._reload()
            except OSError:
                pass
            candidates += self._tokens
        ok = False
        for token in candidates:  # no early exit: constant work per request
            ok |= hmac.compare_digest(presented.encode(), token.encode())
        return ok


MAX_JSON_BODY = 1024 * 1024
MAX_BUNDLE_BODY = 512 * 1024 * 1024


class WorkerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        addr: tuple[str, int],
        store: JobStore,
        token: str | Tokens,
        worker_id: str,
    ) -> None:
        super().__init__(addr, Handler)
        self.store = store
        self.services = ServiceStore(store.root)
        self.tokens = token if isinstance(token, Tokens) else Tokens(fixed=token)
        self.worker_id = worker_id


class UnixWorkerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, store: JobStore, token: str | Tokens, worker_id: str) -> None:
        super().__init__(path, Handler)
        os.chmod(path, 0o600)
        self.store = store
        self.services = ServiceStore(store.root)
        self.tokens = token if isinstance(token, Tokens) else Tokens(fixed=token)
        self.worker_id = worker_id


# Unix sockets have a short path limit (104 bytes on macOS, 108 on Linux).
MAX_SOCKET_PATH = 100


def clear_stale_socket(path: Path) -> None:
    """Remove a socket file left by a dead worker; refuse if one is still serving."""
    if not path.exists():
        return
    probe_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe_sock.connect(str(path))
    except OSError:
        path.unlink()  # nothing listening: stale
        return
    finally:
        probe_sock.close()
    raise OSError(f"another worker is already serving on {path}")


class Handler(BaseHTTPRequestHandler):
    server: Any  # WorkerServer or UnixWorkerServer
    protocol_version = "HTTP/1.1"

    def address_string(self) -> str:
        # Unix-socket peers have no address tuple.
        return str(self.client_address[0]) if self.client_address else "unix"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("NEWTON_WORKER_ACCESS_LOG"):
            super().log_message(format, *args)

    # -- plumbing ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme == "Bearer" and bool(token) and self.server.tokens.check(token)

    def _body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise JobError(413, "request body too large")
        return self.rfile.read(length) if length else b""

    def _dispatch(self, method: str) -> None:
        # Drain the body before any early return so keep-alive stays in sync.
        try:
            if not self._authorized():
                self._body(MAX_BUNDLE_BODY)
                self._error(401, "unauthorized")
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            result = self._route(method, parts, query)
            if result is None:
                self._error(404, "not found")
            elif isinstance(result, bytes):
                self._send(200, result, "application/gzip")
            else:
                self._json(200, result)
        except JobError as e:
            self._error(e.status, e.message)
        except (ValueError, KeyError) as e:
            self._error(400, str(e))
        except Exception as e:  # pragma: no cover - defensive
            self._error(500, f"{type(e).__name__}: {e}")

    def _route(self, method: str, parts: list[str], query: dict[str, str]) -> Optional[Any]:
        store = self.server.store
        if method == "GET" and parts == ["health"]:
            return {
                "ok": True,
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "worker_id": self.server.worker_id,
                "pid": os.getpid(),
                "source_digest": source_digest(),
            }
        if method == "GET" and parts == ["hardware"]:
            return probe(store.root)
        if method == "GET" and parts == ["models", "installed"]:
            return {"models": installed.scan(store.root)}
        if parts == ["jobs"]:
            if method == "GET":
                return store.list()
            if method == "POST":
                return store.create(json.loads(self._body(MAX_JSON_BODY) or b"null"))
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            return store.status(parts[1])
        if len(parts) == 3 and parts[0] == "jobs":
            job_id, action = parts[1], parts[2]
            if method == "PUT" and action == "bundle":
                return store.put_bundle(job_id, self._body(MAX_BUNDLE_BODY))
            if method == "POST" and action == "start":
                return store.start(job_id)
            if method == "POST" and action == "cancel":
                return store.cancel(job_id)
            if method == "GET" and action == "logs":
                return store.logs(
                    job_id,
                    query.get("stream", "stdout"),
                    int(query.get("offset", "0")),
                    int(query.get("limit", "65536")),
                )
            if method == "GET" and action == "artifacts":
                return store.artifacts(job_id)
        return self._service_route(method, parts, query)

    def _service_route(self, method: str, parts: list[str], query: dict[str, str]) -> Optional[Any]:
        services = self.server.services
        if parts == ["services"]:
            if method == "GET":
                return services.list()
            if method == "POST":
                return services.create(
                    json.loads(self._body(MAX_JSON_BODY) or b"null"),
                    # A header, not the spec: the key never lands in spec.json or logs.
                    api_key=self.headers.get("X-Newton-Service-Key"),
                )
        if parts == ["services", "admission"] and method == "POST":
            return services.admission(
                validate_spec(json.loads(self._body(MAX_JSON_BODY) or b"null"))
            )
        if len(parts) == 2 and parts[0] == "services" and method == "GET":
            return services.status(parts[1])
        if len(parts) == 3 and parts[0] == "services":
            service_id, action = parts[1], parts[2]
            if method == "POST" and action == "stop":
                return services.stop(service_id)
            if method == "POST" and action == "drain":
                return services.drain(service_id, float(query.get("seconds", "60")))
            if method == "GET" and action == "logs":
                return services.logs(
                    service_id, int(query.get("offset", "0")), int(query.get("limit", "65536"))
                )
        return None

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")


def worker_id_for(root: Path) -> str:
    path = root / "worker_id"
    if path.exists():
        return path.read_text().strip()
    wid = uuid.uuid4().hex
    path.write_text(wid)
    return wid


def serve(
    root: Path,
    token: str | Tokens,
    host: str = "127.0.0.1",
    port: int = 0,
    allowed_modules: tuple[str, ...] | None = None,
    ready: Optional[threading.Event] = None,
    socket_path: Optional[Path] = None,
) -> Any:
    """Create and return a bound server (Unix socket if socket_path is given,
    else TCP host:port). Call serve_forever() on it."""
    root.mkdir(parents=True, exist_ok=True)
    store = JobStore(root, allowed_modules) if allowed_modules else JobStore(root)
    info: dict[str, Any] = {"pid": os.getpid(), "version": __version__}
    server: Any
    if socket_path is not None:
        if len(str(socket_path)) > MAX_SOCKET_PATH:
            raise OSError(f"socket path too long for a Unix socket: {socket_path}")
        clear_stale_socket(socket_path)
        server = UnixWorkerServer(str(socket_path), store, token, worker_id_for(root))
        info.update(socket=str(socket_path), host=None, port=None)
    else:
        server = WorkerServer((host, port), store, token, worker_id_for(root))
        info.update(socket=None, host=host, port=server.server_address[1])
    write_json_atomic(root / "worker.json", info)
    if ready is not None:
        ready.set()
    return server
