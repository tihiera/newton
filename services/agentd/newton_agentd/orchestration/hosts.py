"""Host registry and runner factory."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import secrets
import time
from collections.abc import Coroutine
from typing import Any

from ..config import Settings
from ..contracts import HostCreate, HostUpdate
from ..runners.base import Runner, RunnerError
from ..runners.bundle import build_bundle
from ..runners.local import LocalRunner
from ..runners.ssh import SshClient, SshRunner, SshTarget
from ..secrets import SecretStore
from ..storage.db import Database, Row, dumps, loads, new_id, now
from .capabilities import capabilities
from .jobs import create_job, force_cancel, selftest_manifest
from .state_machine import SERVICE, record_event

LOCAL_HOST_ID = "local"
DELETED = "deleted"
ACTIVE_JOB_STATES = ("submitting", "running", "collecting")
# Automatic GPU support runs (gpu_support = "auto"): at most one per host per
# minute; each run settles what triggered it (gpu.json then matches the driver and
# CuPy), and failed attempts are retried at most daily.
GPU_AUTO_INTERVAL = 60.0
GPU_RETRY_AFTER_FAILURE = 24 * 3600.0
GPU_PRECONDITION_RECHECK = 3600.0

log = logging.getLogger("newton_agentd.hosts")


class HostNotFound(KeyError):
    pass


class GpuSupportBusy(RunnerError):
    """Jobs are running on the host: packages can't change under them now."""


def gpu_attention(hardware: dict[str, Any] | None) -> str | None:
    """Why an "auto" host's GPU support should run now, or None. Facts come from
    the last probe: the GPU and driver (nvidia-smi), CuPy, and gpu.json."""
    gpus = (hardware or {}).get("gpus") or []
    if not gpus:
        return None
    assert hardware is not None
    support = hardware.get("gpu_support")
    if not support:
        return "not set up yet"
    if support.get("deferred"):
        return "it waited for running jobs"
    if support.get("driver") != gpus[0].get("driver_version"):
        return "the NVIDIA driver changed"
    if support.get("cupy") != (hardware.get("packages") or {}).get("cupy"):
        return "CuPy changed"
    if support.get("ok"):
        return None
    age = time.time() - float(support.get("checked_at") or 0)
    if support.get("attempted", True):
        return "retrying a failed attempt" if age > GPU_RETRY_AFTER_FAILURE else None
    return "re-checking its preconditions" if age > GPU_PRECONDITION_RECHECK else None


class HostKeyUnknown(RunnerError):
    """The host isn't trusted yet: the UI shows these fingerprints with a Trust button."""

    def __init__(self, keys: list[dict[str, str]]) -> None:
        super().__init__(
            "this host's key is not trusted yet; compare the fingerprint with the host "
            "(e.g. `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on it) and trust it",
            transient=False,
            code="hostkey_unknown",
        )
        self.keys = keys


def host_view(row: Row) -> dict[str, Any]:
    out = dict(row)
    out["hardware"] = loads(row["hardware"])
    out["capabilities"] = capabilities(out["hardware"])
    out["use_venv"] = bool(row["use_venv"])
    out["install_deps"] = bool(row["install_deps"])
    out.pop("token_ref", None)
    return out


class HostService:
    def __init__(self, settings: Settings, db: Database, secret_store: SecretStore) -> None:
        self.settings = settings
        self.db = db
        self.secrets = secret_store
        self._runners: dict[str, Runner] = {}
        self._local_token = secrets.token_urlsafe(32)
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task[Any]] = set()
        self._gpu_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._gpu_status: dict[str, dict[str, Any]] = {}
        self._gpu_last_auto: dict[str, float] = {}

    # -- registry -------------------------------------------------------------
    def ensure_local_host(self) -> None:
        if self.get_row(LOCAL_HOST_ID, missing_ok=True) is None:
            t = now()
            self.db.insert(
                "hosts",
                {
                    "id": LOCAL_HOST_ID,
                    "name": "This Mac",
                    "kind": "local",
                    "token_ref": "ephemeral",
                    "max_parallel_jobs": 2,
                    "created_at": t,
                    "updated_at": t,
                },
            )

    def get_row(
        self, host_id: str, missing_ok: bool = False, include_deleted: bool = False
    ) -> Row | None:
        """A deleted host is gone for every caller except history (include_deleted)."""
        row = self.db.query_one("SELECT * FROM hosts WHERE id = ?", (host_id,))
        if row is not None and row["status"] == DELETED and not include_deleted:
            row = None
        if row is None and not missing_ok:
            raise HostNotFound(host_id)
        return row

    def get(self, host_id: str) -> dict[str, Any]:
        row = self.get_row(host_id)
        assert row is not None
        return self._view(row)

    def list_hosts(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM hosts WHERE status != ? ORDER BY created_at", (DELETED,)
        )
        return [self._view(r) for r in rows]

    def _view(self, row: Row) -> dict[str, Any]:
        out = host_view(row)
        task = self._gpu_status.get(row["id"])
        out["gpu_task"] = task
        cuda = out["capabilities"]["cuda"]
        if task and task["state"] == "running" and not cuda["ok"]:
            cuda["reason"] = "installing / checking GPU support now"
        return out

    def create(self, spec: HostCreate) -> dict[str, Any]:
        host_id = new_id("host")
        token_ref = f"worker-token:{host_id}"
        self.secrets.set(token_ref, secrets.token_urlsafe(32))
        t = now()
        with self.db.tx():
            self.db.insert(
                "hosts",
                {
                    "id": host_id,
                    **spec.model_dump(),
                    "use_venv": int(spec.use_venv),
                    "install_deps": int(spec.install_deps),
                    "token_ref": token_ref,
                    "created_at": t,
                    "updated_at": t,
                },
            )
            record_event(self.db, "host", host_id, "created", {"name": spec.name})
        return self.get(host_id)

    async def delete(self, host_id: str, force: bool = False) -> None:
        if host_id == LOCAL_HOST_ID:
            raise ValueError("the local host cannot be deleted")
        row = self.get_row(host_id)
        assert row is not None
        active = self.db.query(
            "SELECT id FROM jobs WHERE host_id = ? AND state IN "
            "('pending_approval','queued','submitting','running','collecting')",
            (host_id,),
        )
        services = self.db.query(
            "SELECT id FROM services WHERE host_id = ? AND state IN "
            "('awaiting_approval','approved','starting','ready','draining','stopping')",
            (host_id,),
        )
        if (active or services) and not force:
            raise ValueError(
                f"host has {len(active)} active job(s) and {len(services)} model service(s) "
                "(stop them, or delete with force=true to abandon them)"
            )
        for job in active:
            await self.cancel_remote(job["id"])  # best effort: don't leave GPU jobs running
        for service in services:  # best effort too: a model server holds GPU memory
            with contextlib.suppress(RunnerError, TimeoutError, OSError):
                runner = await self.runner(host_id)
                call = getattr(runner, "worker_json", None)
                if call is not None:
                    await asyncio.wait_for(
                        call("POST", f"/services/{service['id']}/stop"), timeout=10
                    )
        # Archived, not removed: jobs and experiments keep pointing at it (history,
        # reports), and the name is freed for a new host. Marked deleted *before* the
        # runner goes, so nothing can build a new runner for it in between.
        with self.db.tx():
            self.db.execute(
                "UPDATE hosts SET status = ?, name = ?, hardware = NULL, updated_at = ? "
                "WHERE id = ?",
                (DELETED, f"{row['name']} [deleted {host_id}]", now(), host_id),
            )
            # Services that never started: cancelled here, with their approvals and
            # keys (started ones are marked lost by the service loop).
            for service in self.db.query(
                "SELECT * FROM services WHERE host_id = ? AND state IN "
                "('awaiting_approval', 'approved')",
                (host_id,),
            ):
                gone = {"finished_at": now(), "error": "its host was deleted"}
                SERVICE.transition(self.db, service["id"], service["state"], "cancelled", gone)
                self.db.execute(
                    "UPDATE approvals SET status = 'rejected', decided_at = ?, "
                    "decision_note = 'its host was deleted' WHERE subject_type = 'service' "
                    "AND subject_id = ? AND status = 'pending'",
                    (now(), service["id"]),
                )
                if service["api_key_ref"]:
                    with contextlib.suppress(Exception):
                        self.secrets.delete(service["api_key_ref"])
            for job in active:
                force_cancel(self.db, job["id"], "host deleted; remote state unknown")
            record_event(self.db, "host", host_id, "deleted", {"forced": force})
        await self._drop_runner(host_id)
        self.secrets.delete(row["token_ref"])

    async def cancel_remote(self, job_id: str, timeout: float = 5.0) -> bool:
        """Ask the worker to stop a job; True if it confirmed. Never raises."""
        job = self.db.query_one(
            "SELECT host_id, remote_id, state FROM jobs WHERE id = ?", (job_id,)
        )
        if job is None or not job["remote_id"] or job["state"] not in ("running", "submitting"):
            return False
        try:
            runner = await self.runner(job["host_id"])
            await asyncio.wait_for(runner.cancel(job["remote_id"]), timeout)
        except (RunnerError, TimeoutError, OSError):
            return False
        return True

    async def refresh_stale_online(self, ttl: float = 300.0, timeout: float = 20.0) -> None:
        """Re-check remote hosts that claim "online" but weren't heard from lately."""
        stale = [
            h["id"]
            for h in self.list_hosts()
            if h["kind"] != "local"
            and h["status"] == "online"
            and now() - (h["last_checked_at"] or 0) > ttl
        ]

        async def recheck(host_id: str) -> None:
            try:
                await asyncio.wait_for(self.check(host_id), timeout)
            except (RunnerError, TimeoutError):
                self._update(host_id, status="error:unreachable")

        await asyncio.gather(*(recheck(h) for h in stale))

    def note_reachability(self, host_id: str, error: RunnerError | None) -> None:
        """Keep `status` honest between explicit checks: the scheduler reports what
        it sees, so auto placement doesn't trust a stale "online"."""
        row = self.get_row(host_id, missing_ok=True)
        if row is None or row["kind"] == "local":
            return
        if error is None:
            if row["status"] != "online":
                self._update(host_id, status="online", last_error=None, last_checked_at=now())
            elif now() - (row["last_checked_at"] or 0) > 60:
                self._update(host_id, last_checked_at=now())
        elif (
            error is not None
            and error.code in ("unreachable", "timeout", "ssh_error")
            and (row["status"] == "online")
        ):
            self._update(host_id, status=f"error:{error.code}", last_error=str(error))

    def _update(self, host_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(  # never resurrects a deleted host
            f"UPDATE hosts SET {assignments} WHERE id = :_id AND status != :_deleted",  # noqa: S608
            {**fields, "_id": host_id, "_deleted": DELETED},
        )

    # -- runners --------------------------------------------------------------
    def _token(self, row: Row) -> str:
        token = self.secrets.get(row["token_ref"])
        if not token:
            raise RunnerError("worker token missing from keychain", transient=False, code="token")
        return token

    def ssh_client(self, row: Row) -> SshClient:
        return SshClient(
            SshTarget(row["ssh_target"], row["ssh_user"], row["ssh_port"]),
            self.settings.known_hosts_path,
            ssh=self.settings.ssh_executable,
            ssh_keyscan=self.settings.ssh_keyscan_executable,
            log_dir=self.settings.data_dir / "ssh",
        )

    async def runner(self, host_id: str) -> Runner:
        async with self._lock:
            row = self.get_row(host_id, include_deleted=True)
            assert row is not None
            if row["status"] == DELETED:
                stale = self._runners.pop(host_id, None)
                if stale is not None:
                    await stale.close()
                raise RunnerError("this host was deleted", transient=False, code="host_deleted")
            if host_id in self._runners:
                return self._runners[host_id]
            runner: Runner
            if row["kind"] == "local":
                runner = LocalRunner(
                    host_id,
                    self.settings.local_worker_root,
                    self._local_token,
                    self.settings.worker_source_dir,
                )
            else:
                runner = SshRunner(
                    host_id,
                    self.ssh_client(row),
                    self._token(row),
                    self.settings.worker_source_dir,
                    python=row["python"],
                    use_venv=bool(row["use_venv"]),
                    install_deps=bool(row["install_deps"]),
                    on_event=functools.partial(self._record_runner_event, host_id),
                )
            self._runners[host_id] = runner
            return runner

    def _record_runner_event(self, host_id: str, kind: str, data: dict[str, Any]) -> None:
        record_event(self.db, "host", host_id, kind, data)
        if kind in ("worker_upgraded", "worker_restarted"):
            # The runner bootstrapped by itself (reboot, upgrade): refresh what we
            # know about the host, then see whether GPU support needs a run.
            self._spawn(self._after_worker_change(host_id), f"newton-refresh-{host_id}")

    async def _after_worker_change(self, host_id: str) -> None:
        self._gpu_last_auto.pop(host_id, None)  # if jobs run now, the next idle tick acts
        with contextlib.suppress(RunnerError, HostNotFound):
            await self.check(host_id)
            self.maintain_gpu(host_id, now_=True)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _drop_runner(self, host_id: str) -> None:
        async with self._lock:
            runner = self._runners.pop(host_id, None)
        if runner is not None:
            await runner.close()

    async def close_all(self) -> None:
        background = list(self._background)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        async with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for r in runners:
            await r.close()

    # -- operations -----------------------------------------------------------
    async def check(self, host_id: str) -> dict[str, Any]:
        """Connectivity + worker health + hardware; persisted on the host row."""
        self.get_row(host_id)  # 404 for unknown or deleted hosts
        try:
            runner = await self.runner(host_id)
            retry_now = getattr(runner, "retry_now", None)
            if retry_now is not None:
                retry_now()  # an explicit check retries now instead of waiting out a backoff
            health = await runner.health()
            hardware = await runner.hardware()
        except RunnerError as e:
            self._update(
                host_id, status=f"error:{e.code}", last_error=str(e), last_checked_at=now()
            )
            record_event(self.db, "host", host_id, "check_failed", {"code": e.code})
            raise
        pending = getattr(runner, "upgrade_pending", None)
        self._update(
            host_id,
            status="online",
            hardware=dumps({**hardware, "worker": health}),
            # Online through the old worker, but tell the user why it isn't current.
            last_error=f"worker upgrade pending ({pending}); retrying with backoff"
            if pending
            else None,
            last_checked_at=now(),
        )
        record_event(self.db, "host", host_id, "checked", {"gpus": len(hardware.get("gpus", []))})
        return self.get(host_id)

    async def ensure_checked(self, host_id: str) -> None:
        """Probe a host that has never been checked, so placement has facts."""
        row = self.get_row(host_id)
        assert row is not None
        if row["hardware"] is None:
            try:
                await self.check(host_id)
            except RunnerError:
                pass  # placement will explain that the host can't be used

    def _ssh_row(self, host_id: str) -> Row:
        row = self.get_row(host_id)
        assert row is not None
        if row["kind"] == "local":
            raise ValueError("operation only applies to SSH hosts")
        return row

    async def scan_host_keys(self, host_id: str) -> list[dict[str, str]]:
        keys = await self.ssh_client(self._ssh_row(host_id)).scan_host_keys()
        return [{"type": k.key_type, "fingerprint": k.fingerprint} for k in keys]

    async def trust_host_keys(self, host_id: str, fingerprints: list[str]) -> list[dict[str, str]]:
        keys = await self.ssh_client(self._ssh_row(host_id)).trust_host_keys(fingerprints)
        record_event(
            self.db,
            "host",
            host_id,
            "hostkey_trusted",
            {"fingerprints": [k.fingerprint for k in keys]},
        )
        return [{"type": k.key_type, "fingerprint": k.fingerprint} for k in keys]

    async def connect(self, host_id: str) -> dict[str, Any]:
        """One call from "picked a host" to "ready": verify the host key is trusted
        (else raise HostKeyUnknown with its fingerprints), install/start the
        worker, check it, and queue a selftest job. Returns host + selftest job id."""
        row = self._ssh_row(host_id)
        try:
            await self.ssh_client(row).check()
        except RunnerError as e:
            if e.code != "hostkey_unknown":
                self._update(
                    host_id, status=f"error:{e.code}", last_error=str(e), last_checked_at=now()
                )
                raise
            keys = await self.scan_host_keys(host_id)
            self._update(host_id, status="error:hostkey_unknown", last_checked_at=now())
            raise HostKeyUnknown(keys) from e
        host = await self.bootstrap(host_id)
        job_id = create_job(
            self.db,
            self.settings,
            host_id=host_id,
            role="selftest",
            label="selftest",
            manifest=selftest_manifest(),
            bundle=build_bundle(None, {}),
        )
        record_event(self.db, "host", host_id, "connected", {"selftest_job_id": job_id})
        return {"host": host, "selftest_job_id": job_id}

    async def update(self, host_id: str, body: HostUpdate) -> dict[str, Any]:
        """Change host settings; the next connection uses them."""
        row = self.get_row(host_id)
        assert row is not None
        fields = body.model_dump(exclude_none=True)
        if row["kind"] == "local" and "gpu_support" in fields:
            raise ValueError("GPU support on this Mac comes with the app (MLX), not a setting")
        if fields:
            self._update(host_id, **fields)
            record_event(self.db, "host", host_id, "updated", fields)
            await self._drop_runner(host_id)
            if fields.get("gpu_support") == "auto":
                self.maintain_gpu(host_id, now_=True)
        return self.get(host_id)

    # -- GPU support -------------------------------------------------------------
    def gpu_busy(self, host_id: str) -> bool:
        """A GPU support run is changing the host's packages: start no job there."""
        task = self._gpu_tasks.get(host_id)
        return task is not None and not task.done()

    def start_gpu_support(self, host_id: str, mode: str) -> asyncio.Task[dict[str, Any]]:
        task = self._gpu_tasks.get(host_id)
        if task is not None and not task.done():
            return task
        self._gpu_status[host_id] = {"state": "running", "mode": mode, "started_at": now()}
        task = self._spawn(self._gpu_run(host_id, mode), f"newton-gpu-{host_id}")
        self._gpu_tasks[host_id] = task
        return task

    def upgrade_blocked(self, host_id: str) -> bool:
        """The host runs an older worker whose upgrade failed and isn't due for a retry:
        jobs there can't be submitted for now."""
        runner = self._runners.get(host_id)
        return bool(getattr(runner, "protocol_mismatch", False)) and time.monotonic() < float(
            getattr(runner, "_upgrade_retry_at", 0.0)
        )

    def maintain_gpu(
        self, host_id: str | None = None, now_: bool = False, skip: set[str] | None = None
    ) -> None:
        """Start automatic GPU support runs that are due (scheduler tick, or right
        after a connect/restart/upgrade/setting change for one host). Never while
        the host runs jobs: the next tick after they finish picks it up."""
        rows = self.db.query(
            "SELECT * FROM hosts WHERE kind = 'ssh' AND status = 'online' "
            "AND gpu_support = 'auto' AND (? IS NULL OR id = ?)",
            (host_id, host_id),
        )
        t = time.monotonic()
        for row in rows:
            hid = row["id"]
            reason = gpu_attention(loads(row["hardware"]))
            if reason is None or self.gpu_busy(hid) or hid in (skip or ()):
                continue
            if not now_ and t - self._gpu_last_auto.get(hid, -1e18) < GPU_AUTO_INTERVAL:
                continue
            if self.db.query_one(
                "SELECT 1 FROM jobs WHERE host_id = ? AND state IN (?, ?, ?)",
                (hid, *ACTIVE_JOB_STATES),
            ):
                continue
            self._gpu_last_auto[hid] = t
            record_event(self.db, "host", hid, "gpu_support_due", {"reason": reason})
            self.start_gpu_support(hid, "auto")

    async def _gpu_run(self, host_id: str, mode: str) -> dict[str, Any]:
        """One GPU support run over SSH (the worker keeps serving meanwhile).
        Never raises: returns {"state": ..., ...} and records the outcome."""
        started = now()
        try:
            row = self._ssh_row(host_id)
            state = await self.ssh_client(row).gpu_support(
                mode, row["python"], bool(row["use_venv"]), bool(row["install_deps"])
            )
        except (RunnerError, HostNotFound, ValueError) as e:
            code = getattr(e, "code", "error")
            status = {"state": "failed", "mode": mode, "started_at": started,
                      "finished_at": now(), "code": code, "error": str(e)[-2000:]}  # fmt: skip
            self._gpu_status[host_id] = status
            with contextlib.suppress(Exception):
                record_event(self.db, "host", host_id, "gpu_support",
                             {"ok": False, "mode": mode, "code": code})  # fmt: skip
            return status
        except asyncio.CancelledError:
            self._gpu_status.pop(host_id, None)
            raise
        except Exception as e:  # never let a background run die silently
            log.exception("GPU support run for %s failed", host_id)
            status = {"state": "failed", "mode": mode, "started_at": started,
                      "finished_at": now(), "code": "error", "error": repr(e)}  # fmt: skip
            self._gpu_status[host_id] = status
            return status
        summary = {k: (state or {}).get(k) for k in ("ok", "dist", "cupy", "driver", "error")}
        record_event(self.db, "host", host_id, "gpu_support", {"mode": mode, **summary})
        if mode == "cuda" and summary["ok"] and row["gpu_support"] == "off":
            self._update(host_id, gpu_support="auto")  # the user asked for GPU support
            record_event(self.db, "host", host_id, "updated", {"gpu_support": "auto"})
        with contextlib.suppress(RunnerError, HostNotFound):
            await self.check(host_id)  # the host row now shows the new gpu.json
        status = {"state": "done", "mode": mode, "started_at": started, "finished_at": now(),
                  "ok": bool(summary["ok"])}  # fmt: skip
        self._gpu_status[host_id] = status
        return status

    async def install_gpu_support(self, host_id: str, wait: bool = False) -> dict[str, Any]:
        """The "Install GPU support" action: install CuPy for the host's driver (or
        re-verify the one there) now, even if an automatic attempt failed before.
        Without `wait` it returns at once (watch `gpu_task` / events); with it,
        raises with the host's own reason when the GPU still can't be used."""
        self._ssh_row(host_id)
        running = self._gpu_tasks.get(host_id)
        if running is not None and not running.done():
            if self._gpu_status.get(host_id, {}).get("mode") == "cuda":
                if wait:
                    await asyncio.shield(running)
                return await self._gpu_result(host_id, wait)
            await asyncio.shield(running)  # an automatic run: let it finish first
        task = self.start_gpu_support(host_id, "cuda")
        if wait:
            await asyncio.shield(task)
        return await self._gpu_result(host_id, wait)

    async def _gpu_result(self, host_id: str, waited: bool) -> dict[str, Any]:
        host = self.get(host_id)
        if not waited:
            return host
        status = self._gpu_status.get(host_id) or {}
        if status.get("state") == "failed":
            cls = GpuSupportBusy if status.get("code") == "bootstrap_busy" else RunnerError
            raise cls(status.get("error") or "GPU support failed", transient=True,
                      code=status.get("code") or "error")  # fmt: skip
        cuda = host["capabilities"]["cuda"]
        if not cuda["ok"]:
            raise RunnerError(
                f"GPU support is not usable on {host['name']}: {cuda.get('reason')}",
                transient=False,
                code="gpu_support_failed",
            )
        return host

    async def bootstrap(self, host_id: str) -> dict[str, Any]:
        row = self._ssh_row(host_id)
        await self._drop_runner(host_id)
        client = self.ssh_client(row)
        try:
            await client.check()
            status = await client.bootstrap(
                self.settings.worker_source_dir,
                self._token(row),
                row["python"],
                bool(row["use_venv"]),
                bool(row["install_deps"]),
            )
        except RunnerError as e:
            self._update(
                host_id, status=f"error:{e.code}", last_error=str(e), last_checked_at=now()
            )
            record_event(self.db, "host", host_id, "bootstrap_failed", {"code": e.code})
            raise
        record_event(self.db, "host", host_id, "bootstrapped", status)
        host = await self.check(host_id)
        # GPU support runs separately, once the worker serves: a slow CuPy download
        # never holds up (or fails) the connection itself.
        self.maintain_gpu(host_id, now_=True)
        return self.get(host_id) if self.gpu_busy(host_id) else host
