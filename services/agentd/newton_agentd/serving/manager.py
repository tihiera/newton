"""Model services managed by agentd (SV2).

A service is a model server the worker runs on a host (see the worker's
services.py). agentd keeps the record and its state machine, asks for approval
when starting one means a download or model-supplied code, keeps the API key in
the secret store (the Keychain on macOS; the database holds only a reference),
and makes a ready service reachable on this Mac:

  - SSH host: a forward from 127.0.0.1:<local_port> here to the service's port on
    the host, over its own private ControlMaster (loopback at both ends);
  - this Mac's local worker: the service's own 127.0.0.1 port, no forward.

The API key is generated here and stored *before* the worker is asked to start the
service (it travels in a request header), so a lost or retried request can't lose
it. A reconcile loop moves each service along: approved -> start it on the worker;
starting/ready/draining/stopping -> mirror the worker's state, keep the forward up
while it serves, drop it when it ends. Each service is advanced in its own task,
so one slow host never holds up the others. agentd restarts are harmless: the loop
picks every record up again, and ends ssh masters a crashed agentd left behind.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
import os
import secrets as token_source
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..config import Settings
from ..contracts import ServiceCreate, ServiceSpec
from ..orchestration.approvals import Approvals
from ..orchestration.hosts import HostNotFound, HostService
from ..orchestration.state_machine import SERVICE, ConcurrentTransition, record_event
from ..profile import Profile
from ..runners.base import RunnerError
from ..runners.ssh import Tunnel
from ..secrets import SecretStore
from ..storage.db import Database, Row, dumps, loads, new_id, now

log = logging.getLogger("newton_agentd.services")

APPROVAL_KIND = "start_service"
ACTIVE = ("approved", "starting", "ready", "draining", "stopping")
SERVING = ("ready", "draining")  # reachable while in these states
KEYED_ENGINES = ("vllm", "fake")  # engines that check an API key (Ollama can't)
POLL_EVERY = 2.0  # seconds between worker checks per service
BACKOFF_MAX = 60.0  # seconds, for a host that keeps failing
# An approved service whose host is down (or this Mac offline) waits for it: never
# failed for that. Its start is retried after 5 s, doubling up to 5 min, and at once
# when the host comes back online.
HOST_WAIT_START = 5.0
HOST_WAIT_MAX = 300.0
PROBE_EVERY = 10.0  # seconds between endpoint probes on this Mac
REBUILD_AFTER = 2  # consecutive connect failures before a live forward is rebuilt
# The worker's state -> ours (the worker has a separate "loading" phase).
FROM_WORKER = {
    "starting": "starting", "loading": "starting", "ready": "ready", "draining": "draining",
    "stopping": "stopping", "stopped": "stopped", "failed": "failed", "lost": "lost",
}  # fmt: skip


class ServiceNotFound(KeyError):
    pass


class ServiceRefused(Exception):
    """The host can't take this service now (memory, disk, engine): nothing was created."""


def port_free(port: int | None) -> bool:
    """Could ssh bind it? SO_REUSEADDR like ssh's own listener, so a port with only
    old connections in TIME_WAIT counts as free and the endpoint keeps its port."""
    if not port:
        return False
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def command_line(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


class ServiceManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        hosts: HostService,
        approvals: Approvals,
        secrets: SecretStore,
        profile: Profile | None = None,
    ) -> None:
        self.profile = profile
        self.settings = settings
        self.db = db
        self.hosts = hosts
        self.approvals = approvals
        self.secrets = secrets
        approvals.register(APPROVAL_KIND, self._on_decision)
        self._tunnels: dict[str, Tunnel] = {}
        self._next_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._last_probe: dict[str, float] = {}
        self._connect_failures: dict[str, int] = {}
        self._reachable: dict[str, bool] = {}
        self._progress: dict[str, dict[str, Any]] = {}  # service_id -> download progress
        self._forward_denied: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._busy: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        # Set by the router (SV3): told about every change in what can serve, and asked
        # whether a host is leased to a timed run (no model starts loading there then).
        self.on_change: Callable[[], None] = lambda: None
        self.is_leased: Callable[[str], bool] = lambda host_id: False
        self.on_late_start: Callable[[str, str], None] = lambda host_id, service_id: None
        self._starting: dict[str, str] = {}  # service_id -> host_id: a start on its way
        self._host_down: set[str] = set()  # approved services waiting for their host
        hosts.add_online_listener(self._host_online)

    # -- records -------------------------------------------------------------------
    def get_row(self, service_id: str) -> Row:
        row = self.db.query_one("SELECT * FROM services WHERE id = ?", (service_id,))
        if row is None:
            raise ServiceNotFound(service_id)
        return row

    def _progress_from(self, service_id: str, raw: Any) -> None:
        """The worker's download progress ({phase, completed, total} bytes), kept in
        memory only: it matters while it lasts."""
        ok = isinstance(raw, dict) and isinstance(raw.get("completed"), int)
        if ok:
            total = raw.get("total")
            self._progress[service_id] = {
                "phase": str(raw.get("phase") or "downloading")[:20],
                "completed": int(raw["completed"]),
                "total": int(total) if isinstance(total, int) and total > 0 else None,
            }
        else:
            self._progress.pop(service_id, None)

    def view(self, row: Row) -> dict[str, Any]:
        out = dict(row)
        ended = row["state"] in SERVICE.terminal
        out["progress"] = None if ended else self._progress.get(row["id"])  # a download
        out["spec"] = loads(row["spec"])
        out["healthy"] = None if row["healthy"] is None else bool(row["healthy"])
        for private in ("api_key_ref", "forward_pid", "forward_control"):
            out.pop(private, None)
        serving = row["state"] in SERVING and row["local_port"]
        out["endpoint"] = (
            {
                "base_url": f"http://127.0.0.1:{row['local_port']}/v1",
                "auth": "bearer" if row["api_key_ref"] else "none",
                "reachable": self._reachable.get(row["id"]),
            }
            if serving
            else None
        )
        if out["spec"]["engine"] in ("ollama", "mlx"):
            out["auth_note"] = (
                f"{out['spec']['engine']} has no API keys: anything running on this Mac (any "
                "local user) can use the endpoint directly while it is up; it is not reachable "
                "from the network. Use the router (its key) instead"
            )
        return out

    def get(self, service_id: str) -> dict[str, Any]:
        return self.view(self.get_row(service_id))

    def list(self, host_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM services WHERE (? IS NULL OR host_id = ?) ORDER BY created_at",
            (host_id, host_id),
        )
        return [self.view(r) for r in rows]

    def credentials(self, service_id: str) -> dict[str, Any]:
        """The endpoint and its key, for a client on this Mac (agentd-token protected)."""
        row = self.get_row(service_id)
        if row["state"] not in SERVING or not row["local_port"]:
            raise ValueError(f"service {service_id} is {row['state']}, not serving")
        key = self.secrets.get(row["api_key_ref"]) if row["api_key_ref"] else None
        return {
            "base_url": f"http://127.0.0.1:{row['local_port']}/v1",
            "api_key": key,
            "model": loads(row["spec"])["model"],
        }

    def reachable(self, service_id: str) -> bool | None:
        """What the last probe from this Mac saw (None: not probed yet)."""
        return self._reachable.get(service_id)

    def mark_unreachable(self, service_id: str) -> None:
        """The router couldn't connect: re-probe now (and rebuild the forward if the
        probe agrees) instead of waiting for the next 10 s check."""
        if self._reachable.get(service_id) is not False:
            self._reachable[service_id] = False
            record_event(self.db, "service", service_id, "unreachable", {"by": "router"})
        self._last_probe.pop(service_id, None)
        self.wake(service_id)

    def _host_online(self, host_id: str) -> None:
        """A host is back: its services are advanced now, not after their backoff."""
        for row in self.db.query(
            f"SELECT id FROM services WHERE host_id = ? AND state IN "  # noqa: S608
            f"({', '.join('?' * len(ACTIVE))})",
            (host_id, *ACTIVE),
        ):
            self._failures.pop(row["id"], None)
            self.wake(row["id"])

    def starting_on(self, host_id: str) -> "builtins.list[str]":
        """Services whose start request is on its way to this host right now."""
        return [sid for sid, host in self._starting.items() if host == host_id]

    def reprobe(self, service_id: str) -> None:
        """Check this service now, without assuming it's down (one broken connection
        doesn't mean the engine is)."""
        self._last_probe.pop(service_id, None)
        self.wake(service_id)

    # -- requests ------------------------------------------------------------------------
    async def worker(self, host_id: str, method: str, path: str, **kwargs: Any) -> Any:
        runner = await self.hosts.runner(host_id)
        call = getattr(runner, "worker_json", None)
        if call is None:
            raise RunnerError("this host's runner can't run services", transient=False,
                              code="unsupported")  # fmt: skip
        return await call(method, path, **kwargs)

    async def create(self, body: ServiceCreate) -> dict[str, Any]:
        host = self.hosts.get_row(body.host_id)
        assert host is not None
        if body.settings.engine == "mlx":
            if host["kind"] != "local":
                raise ServiceRefused("MLX models run on this Mac only")
            if self.profile is None or not self.profile.mac_models():
                raise ServiceRefused("models on this Mac are off: turn on mac_models in the "
                                     "profile first")  # fmt: skip
        service_id = new_id("svc")
        spec = ServiceSpec(service_id=service_id, **body.settings.model_dump())
        try:
            admission = await self.worker(
                host["id"], "POST", "/services/admission", json=spec.model_dump(), timeout=60
            )
        except RunnerError as e:
            if e.code.startswith("http_4"):
                raise ServiceRefused(str(e)) from e
            raise
        refuse_unless_admitted(admission, spec, host)
        download = not admission.get("model_present", True)
        needs_approval = download or spec.trust_remote_code
        key_ref = None
        if spec.engine in KEYED_ENGINES:
            key_ref = f"service-key-{service_id}"
            self.secrets.set(key_ref, token_source.token_urlsafe(32))  # stored before any use
        t = now()
        with self.db.tx():
            self.db.insert(
                "services",
                {
                    "id": service_id,
                    "host_id": host["id"],
                    "name": body.name,
                    "spec": dumps(spec.model_dump()),
                    "state": "awaiting_approval" if needs_approval else "approved",
                    "api_key_ref": key_ref,
                    "created_at": t,
                    "updated_at": t,
                },
            )
            record_event(self.db, "service", service_id, "created",
                         {"host_id": host["id"], "model": spec.model,
                          "needs_approval": needs_approval})  # fmt: skip
            if needs_approval:
                reasons = []
                if download:
                    reasons.append(f"downloads {spec.model} (up to {spec.memory_gb:.1f} GB)")
                if spec.trust_remote_code:
                    reasons.append("runs code shipped with the model (trust_remote_code)")
                self.approvals.request(
                    APPROVAL_KIND,
                    "service",
                    service_id,
                    f"Start model service {body.name} on {host['name']}: " + "; ".join(reasons),
                    {
                        "host": {"id": host["id"], "name": host["name"]},
                        "engine": spec.engine,
                        "model": spec.model,
                        "revision": spec.revision,
                        "download": download,
                        "download_gb_estimate": spec.memory_gb if download else 0,
                        "trust_remote_code": spec.trust_remote_code,
                        "memory_gb": spec.memory_gb,
                        "context_length": spec.context_length,
                        "parallel": spec.parallel,
                        "admission": admission,
                    },
                )
        self.wake(service_id)
        return self.get(service_id)

    def _on_decision(self, db: Database, approval: Row, approved: bool) -> None:
        sid = approval["subject_id"]
        if approved:
            SERVICE.transition(db, sid, "awaiting_approval", "approved")
        else:
            SERVICE.transition(db, sid, "awaiting_approval", "rejected",
                               {"finished_at": now()})  # fmt: skip
            self._forget_key(db.query_one("SELECT * FROM services WHERE id = ?", (sid,)))
        self.wake(sid)

    async def stop(self, service_id: str) -> dict[str, Any]:
        row = self.get_row(service_id)
        if row["state"] in ("awaiting_approval", "approved"):
            if row["state"] == "awaiting_approval":
                self._reject_pending_approval(service_id)
            self._move(row, "cancelled", finished_at=now())
            self._finished(self.get_row(service_id))
            if row["state"] == "approved":
                # A start may be on its way to the worker right now: ask it to stop
                # too (404 if it never arrived; _start_on_worker also checks after).
                with contextlib.suppress(RunnerError):
                    await self.worker(row["host_id"], "POST", f"/services/{service_id}/stop")
            return self.get(service_id)
        if row["state"] not in ("starting", "ready", "draining"):
            return self.view(row)
        remote = await self.worker(row["host_id"], "POST", f"/services/{service_id}/stop")
        self._move(self.get_row(service_id), "stopping", remote_state=remote.get("state"))
        self.wake(service_id)
        return self.get(service_id)

    async def drain(self, service_id: str, seconds: float) -> dict[str, Any]:
        row = self.get_row(service_id)
        if row["state"] != "ready":
            raise ValueError(f"only a ready service can drain (it is {row['state']})")
        remote = await self.worker(row["host_id"], "POST", f"/services/{service_id}/drain",
                                   params={"seconds": seconds})  # fmt: skip
        self._move(row, "draining", remote_state=remote.get("state"))
        return self.get(service_id)

    async def logs(self, service_id: str, offset: int) -> dict[str, Any]:
        row = self.get_row(service_id)
        empty = {"offset": 0, "next_offset": 0, "data": "", "eof": True}
        if row["remote_state"] is None:  # never reached the worker
            return empty
        try:
            out: dict[str, Any] = await self.worker(
                row["host_id"], "GET", f"/services/{service_id}/logs", params={"offset": offset}
            )
        except RunnerError as e:
            if e.code == "http_404":
                return empty
            raise
        return out

    def _reject_pending_approval(self, service_id: str) -> None:
        self.db.execute(
            "UPDATE approvals SET status = 'rejected', decided_at = ?, "
            "decision_note = 'the service was cancelled before approval' "
            "WHERE subject_type = 'service' AND subject_id = ? AND status = 'pending'",
            (now(), service_id),
        )

    def _move(self, row: Row, dst: str, **fields: Any) -> bool:
        if row["state"] == dst:
            if fields:
                self._update(row["id"], **fields)
            return True
        try:
            SERVICE.transition(self.db, row["id"], row["state"], dst, fields)
        except ConcurrentTransition:
            return False
        self.on_change()
        return True

    def _update(self, service_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(
            f"UPDATE services SET {assignments} WHERE id = :_id",  # noqa: S608
            {**fields, "_id": service_id},
        )

    def _forget_key(self, row: Row | None) -> None:
        if row and row["api_key_ref"]:
            with contextlib.suppress(Exception):
                self.secrets.delete(row["api_key_ref"])
            self.db.execute("UPDATE services SET api_key_ref = NULL WHERE id = ?", (row["id"],))

    def _finished(self, row: Row) -> None:
        """A service reached a terminal state: no key left behind, no bookkeeping."""
        if row["state"] not in SERVICE.terminal:
            return
        self._forget_key(row)
        sid = row["id"]
        self._next_at.pop(sid, None)
        self._failures.pop(sid, None)
        self._last_probe.pop(sid, None)
        self._connect_failures.pop(sid, None)
        self._reachable.pop(sid, None)
        self._forward_denied.discard(sid)
        self._host_down.discard(sid)

    # -- the reconcile loop -------------------------------------------------------------------
    def start(self) -> None:
        self._end_orphaned_forwards()
        self._task = asyncio.create_task(self._run(), name="newton-services")

    def wake(self, service_id: str | None = None) -> None:
        if service_id is not None:
            self._next_at.pop(service_id, None)  # act on it at once
        self._wake.set()

    async def stop_loop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for sid in list(self._tunnels):
            await self._close_forward(sid)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("service reconcile pass failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_EVERY / 2)

    async def tick(self) -> None:
        """Start a pass for every service that is due, without waiting for them:
        each runs on its own, and a slow host only delays its own services."""
        rows = self.db.query(
            f"SELECT id FROM services WHERE state IN ({', '.join('?' * len(ACTIVE))})",  # noqa: S608
            ACTIVE,
        )
        t = time.monotonic()
        for row in rows:
            sid = row["id"]
            if sid in self._busy or t < self._next_at.get(sid, 0.0):
                continue
            self._busy.add(sid)
            task = asyncio.create_task(self._advance_guarded(sid), name=f"newton-service-{sid}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        for sid in list(self._tunnels):  # forwards of services that ended go away
            current = self.db.query_one("SELECT state FROM services WHERE id = ?", (sid,))
            if current is None or current["state"] not in SERVING:
                await self._close_forward(sid)

    async def _advance_guarded(self, service_id: str) -> None:
        ok = False
        try:
            ok = await self._advance(self.get_row(service_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("service %s: reconcile failed", service_id)
        finally:
            self._busy.discard(service_id)
        current = self.db.query_one("SELECT state FROM services WHERE id = ?", (service_id,))
        if current is None or current["state"] in SERVICE.terminal:
            return  # finished: _finished already dropped its bookkeeping
        # Back off a host that keeps failing; otherwise check again in POLL_EVERY.
        failures = 0 if ok else self._failures.get(service_id, 0) + 1
        self._failures[service_id] = failures
        if failures and service_id in self._host_down:
            delay = min(HOST_WAIT_MAX, HOST_WAIT_START * 2 ** (failures - 1))
        elif failures:
            delay = min(BACKOFF_MAX, POLL_EVERY * 2 ** (failures - 1))
        else:
            delay = POLL_EVERY
        self._next_at[service_id] = time.monotonic() + delay

    async def _advance(self, row: Row) -> bool:
        """One step for one service. False: the host couldn't be reached (back off)."""
        try:
            host = self.hosts.get_row(row["host_id"])
        except HostNotFound:
            host = None
        if host is None:  # the host was deleted under the service
            dst = "failed" if row["state"] == "approved" else "lost"
            self._move(row, dst, error="its host was deleted", finished_at=now())
            await self._close_forward(row["id"])
            self._finished(self.get_row(row["id"]))
            return True
        if row["state"] == "approved":
            return await self._start_on_worker(row)
        try:
            remote = await self.worker(row["host_id"], "GET", f"/services/{row['id']}")
        except RunnerError as e:
            if e.code == "http_404":
                self._move(row, "lost", error="the host has no record of this service",
                           finished_at=now())  # fmt: skip
                await self._close_forward(row["id"])
                self._finished(self.get_row(row["id"]))
                return True
            # A host down marks it (it is then re-checked by itself, and its services
            # are woken the moment it is back), like a job finding it down does.
            self.hosts.note_reachability(row["host_id"], e)
            # Unreachable for now: keep the state, say why, and what this Mac sees.
            self._update(row["id"], error=f"host unreachable: {e}"[:500])
            if row["state"] in SERVING and row["local_port"]:
                await self._probe(row, force=True)
            return False
        self.hosts.note_reachability(row["host_id"], None)
        self._progress_from(row["id"], remote.get("progress"))
        ours: str = FROM_WORKER.get(str(remote.get("state"))) or str(row["state"])
        fields: dict[str, Any] = {
            "remote_state": remote.get("state"),
            "healthy": None if remote.get("healthy") is None else int(bool(remote["healthy"])),
            "remote_port": remote.get("port"),
            "last_seen_at": now(),
            "error": remote.get("error"),
        }
        if ours == "ready" and not row["ready_at"]:
            fields["ready_at"] = now()
        if ours in SERVICE.terminal:
            fields["finished_at"] = now()
        if ours != row["state"] and ours not in SERVICE.transitions.get(row["state"], ()):
            ours = row["state"]  # e.g. the worker says "ready" while we stop: keep stopping
        self._move(row, ours, **fields)
        current = self.get_row(row["id"])
        if current["state"] in SERVING and remote.get("port"):
            await self._ensure_endpoint(current, host)
        elif current["state"] in SERVICE.terminal:
            await self._close_forward(row["id"])
            self._finished(current)
        return True

    async def _start_on_worker(self, row: Row) -> bool:
        if spec_engine(row) == "mlx" and (self.profile is None or not self.profile.mac_models()):
            self._move(row, "cancelled", error="models on this Mac were turned off",
                       finished_at=now())  # fmt: skip
            self._finished(self.get_row(row["id"]))
            return True
        if self.is_leased(row["host_id"]):
            # A timed run has the host: loading a model now would share its memory bus.
            note = "waiting: a timed run has this host to itself"
            if row["error"] != note:
                self._update(row["id"], error=note)
            return True
        spec = loads(row["spec"])
        headers = {}
        if row["api_key_ref"]:
            key = await asyncio.to_thread(self.secrets.get, row["api_key_ref"])
            if not key:
                self._move(row, "failed", error="its API key is missing from the secret store",
                           finished_at=now())  # fmt: skip
                self._finished(self.get_row(row["id"]))
                return True
            headers["X-Newton-Service-Key"] = key
        self._starting[row["id"]] = row["host_id"]  # a lease taken meanwhile counts it
        try:
            started = await self.worker(row["host_id"], "POST", "/services", json=spec,
                                        headers=headers, timeout=60)  # fmt: skip
        except RunnerError as e:
            self.hosts.note_reachability(row["host_id"], e)  # see _advance
            if self.hosts.is_down(row["host_id"], e):
                # The host or this Mac's network is down: wait for it, with backoff.
                self._host_down.add(row["id"])
                self._update(row["id"], error=self.hosts.waiting_note(row["host_id"], e))
                return False
            self._host_down.discard(row["id"])
            if e.transient or e.code == "not_ready":
                self._update(row["id"], error=f"waiting for the host: {e}"[:500])
                return False
            self._move(row, "failed", error=str(e)[:2000], finished_at=now())
            self._finished(self.get_row(row["id"]))
            return True
        finally:
            self._starting.pop(row["id"], None)
        self.hosts.note_reachability(row["host_id"], None)
        self._host_down.discard(row["id"])
        if self.is_leased(row["host_id"]):
            self.on_late_start(row["host_id"], row["id"])  # the timed run's caveat
        moved = self._move(row, "starting", remote_state=started.get("state"),
                           remote_port=started.get("port"), error=None)  # fmt: skip
        if not moved:
            # Cancelled (or its host deleted) while the start was on its way: the
            # worker must not keep a model server nobody tracks.
            current = self.get_row(row["id"])
            if current["state"] in SERVICE.terminal:
                with contextlib.suppress(RunnerError):
                    await self.worker(row["host_id"], "POST", f"/services/{row['id']}/stop")
                self._finished(current)
            return True
        record_event(self.db, "service", row["id"], "started_on_worker",
                     {"port": started.get("port")})  # fmt: skip
        return True

    async def _ensure_endpoint(self, row: Row, host: Row) -> None:
        sid, remote_port = row["id"], int(row["remote_port"])
        if host["kind"] == "local":  # same machine: the service's own loopback port
            if row["local_port"] != remote_port:
                self._update(sid, local_port=remote_port)
            await self._probe(self.get_row(sid))
            return
        if sid in self._forward_denied:
            return
        tunnel = self._tunnels.get(sid)
        if tunnel is not None and (
            not tunnel.alive or getattr(tunnel, "remote_port", None) != remote_port
        ):
            await self._close_forward(sid, keep_state=True)
            tunnel = None
        if tunnel is None:
            # The same port as before whenever possible: clients keep their base_url.
            local = row["local_port"] if port_free(row["local_port"]) else free_local_port()
            try:
                tunnel = await self.hosts.ssh_client(host).open_port_forward(remote_port, local)
            except RunnerError as e:
                self._update(sid, error=f"forward failed: {e}"[:500])
                return
            tunnel.remote_port = remote_port  # type: ignore[attr-defined]
            self._tunnels[sid] = tunnel
            pid = tunnel.proc.pid if tunnel.proc else None
            self._update(sid, local_port=local, forward_pid=pid,
                         forward_control=tunnel.control_path)  # fmt: skip
            record_event(self.db, "service", sid, "forwarded",
                         {"local_port": local, "remote_port": remote_port})  # fmt: skip
            self._last_probe.pop(sid, None)  # check the new forward right away
            self.on_change()
        await self._probe(self.get_row(sid))

    async def _probe(self, row: Row, force: bool = False) -> None:
        """Is the endpoint answering on this Mac? Every 10 s. An HTTP error or a slow
        answer is the engine's business (reported, nothing torn down); only failing to
        connect at all, twice in a row, rebuilds the forward."""
        sid = row["id"]
        t = time.monotonic()
        if not force and t - self._last_probe.get(sid, -1e9) < PROBE_EVERY:
            return
        self._last_probe[sid] = t
        ref = row["api_key_ref"]
        key = await asyncio.to_thread(self.secrets.get, ref) if ref else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        connect_failed = False
        try:
            # trust_env=False: never through an HTTP proxy (it would see the key).
            async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
                resp = await http.get(f"http://127.0.0.1:{row['local_port']}/v1/models",
                                      headers=headers)  # fmt: skip
            reachable = resp.status_code == 200
        except httpx.TimeoutException:
            return  # slow (a busy engine), not broken: what was known stays
        except httpx.TransportError:
            reachable, connect_failed = False, True
        if reachable != self._reachable.get(sid):
            record_event(self.db, "service", sid, "reachable" if reachable else "unreachable")
            self._reachable[sid] = reachable
            self.on_change()
        tunnel = self._tunnels.get(sid)
        if not connect_failed or tunnel is None:
            self._connect_failures.pop(sid, None)
            return
        failures = self._connect_failures.get(sid, 0) + 1
        self._connect_failures[sid] = failures
        if "administratively prohibited" in tunnel.log_text():
            # The host's sshd forbids TCP forwarding: rebuilding won't help.
            self._forward_denied.add(sid)
            self._update(
                sid,
                error="the host's sshd refuses TCP forwarding "
                "(AllowTcpForwarding / PermitOpen): the service can't be reached here",
            )
            await self._close_forward(sid, keep_state=True)
        elif failures >= REBUILD_AFTER or not tunnel.alive:
            self._connect_failures.pop(sid, None)
            await self._close_forward(sid, keep_state=True)  # rebuilt on the next pass

    async def _close_forward(self, service_id: str, keep_state: bool = False) -> None:
        tunnel = self._tunnels.pop(service_id, None)
        if not keep_state:
            self._reachable.pop(service_id, None)
        if tunnel is not None:
            await tunnel.close()
            with contextlib.suppress(Exception):
                self._update(service_id, forward_pid=None, forward_control=None)

    def _end_orphaned_forwards(self) -> None:
        """ssh masters a crashed agentd left running (they hold the local ports)."""
        for row in self.db.query(
            "SELECT id, forward_pid, forward_control FROM services WHERE forward_pid IS NOT NULL"
        ):
            pid, control = row["forward_pid"], row["forward_control"] or ""
            if control and control in command_line(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            self._update(row["id"], forward_pid=None, forward_control=None)


def spec_engine(row: Row) -> str:
    engine: str = loads(row["spec"])["engine"]
    return engine


def refuse_unless_admitted(admission: dict[str, Any], spec: ServiceSpec, host: Row) -> None:
    name = host["name"]
    if admission.get("engine_installed") is False:
        raise ServiceRefused(f"the {spec.engine} engine is not installed on {name}")
    if admission.get("sizing"):
        raise ServiceRefused(f"{spec.model} on {name}: {admission['sizing']}")
    if admission.get("available_gb") is None:
        raise ServiceRefused(f"{name} can't report its available memory")
    if admission.get("disk") is False:
        raise ServiceRefused(
            f"not enough disk on {name} to download {spec.model}: "
            f"{admission.get('disk_free_gb')} GB free"
        )
    if not admission.get("ok"):
        raise ServiceRefused(
            f"not enough memory on {name} for {spec.model}: it needs {admission['need_gb']} GB, "
            f"{admission['available_gb']} GB is available ({admission['pending_gb']} GB "
            f"promised to services still loading), and {admission['reserve_gb']} GB is kept free"
        )
