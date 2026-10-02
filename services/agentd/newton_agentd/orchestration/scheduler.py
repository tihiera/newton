"""Durable scheduler. Everything it knows lives in SQLite, so a restart simply
continues: `submitting` jobs are re-submitted with the same idempotent remote id,
`running` jobs are polled again, `collecting` jobs re-download, and experiments
stuck in `evaluating` are re-evaluated.

Retries are only for infrastructure failures (unreachable worker, lost wrapper).
A job that ran and failed is a result, not something to retry. A host that can't
be reached at all (offline, asleep, its name not resolving while this Mac is off
the network) never fails a job: the job waits for it, with backoff.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..reporting.markdown import write_report
from ..research.evaluation import evaluate
from ..runners.base import RunnerError
from ..runners.bundle import repository_commit
from ..storage.db import Database, Row, dumps, loads, now
from ..storage.files import UnsafeArchive, extract_tar_gz
from .hosts import DELETED, HostService
from .jobs import job_dir, remote_job_id
from .state_machine import EXPERIMENT, JOB, ConcurrentTransition, IllegalTransition, record_event

if TYPE_CHECKING:
    from ..serving.router import Router

log = logging.getLogger("newton_agentd.scheduler")
GPU_BACKENDS = ("cuda", "metal")


def _alone(manifest_json: str) -> bool:
    if _backend(manifest_json) in GPU_BACKENDS:
        return True
    return bool(loads(manifest_json).get("exclusive"))


def _backend(manifest_json: str) -> str:
    backend: str = loads(manifest_json).get("backend", "cpu")
    return "cuda" if backend == "gpu" else backend


WORKER_TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "lost"}
FINAL_FROM_WORKER = {
    "succeeded": "succeeded",
    "failed": "failed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
}
BACKOFF_BASE = 5.0
UPGRADE_WAIT = 60.0
# Waiting for a host that is down: 5 s, doubling up to 5 min (sooner when it's back).
HOST_WAIT_START = 5.0
HOST_WAIT_MAX = 300.0
# A cancel asked of a job that hasn't started: how long the worker gets to confirm.
CANCEL_TIMEOUT = 30.0
UNSTARTED_CANCEL_UNKNOWN = (
    "cancelled while its host couldn't be reached; its remote state is unknown "
    "(Newton asks the host to stop it when it is back)"
)
UNSTARTED_CANCEL_UNCONFIRMED = (
    "cancelled; its host didn't confirm it, so its remote state is unknown "
    "(Newton keeps asking the host to stop it)"
)
# Such a cancel is kept as a 'remote_cancel_pending' event on the job (so a restart
# keeps asking) until a 'remote_cancelled' event settles it. While the host is online
# it is asked again on the next tick, then after 30 s, doubling up to every 15 min;
# a host that is down is asked as soon as it is back.
ORPHAN_RETRY_START = 30.0
ORPHAN_RETRY_MAX = 900.0


class Scheduler:
    def __init__(
        self, settings: Settings, db: Database, hosts: HostService, router: Router | None = None
    ) -> None:
        self.settings = settings
        self.db = db
        self.hosts = hosts
        self.router = router
        self._submit_failures: dict[str, int] = {}
        self._host_waits: dict[str, int] = {}  # job id -> submits that found its host down
        # Cancelled before they started without the host confirming it (it was down, or
        # didn't answer): job id -> (host id, remote id). A submit may have reached the
        # worker before, so it is asked to stop them until it confirms (persisted as
        # events: see _load_orphans). job id -> (failed asks, next ask, monotonic).
        self._orphans: dict[str, tuple[str, str]] = {}
        self._orphan_due: dict[str, tuple[int, float]] = {}
        self._orphan_tasks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._host_tasks: dict[str, asyncio.Task[None]] = {}
        self._host_last_pass: dict[str, float] = {}
        self.ticks = 0
        hosts.add_online_listener(self._host_online)

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._load_orphans()
        self._task = asyncio.create_task(self._run(), name="newton-scheduler")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task is not None:
            await self._task
        # Every transition is persisted as it happens, so cancelling mid-pass is safe:
        # the next start resumes from the database.
        tasks = [*self._host_tasks.values(), *self._orphan_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._host_tasks.clear()

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.scheduler_interval)
            except TimeoutError:
                pass
            self._wake.clear()

    async def tick(self) -> None:
        """Start one pass per host with pending work (unless that host's previous
        pass is still running), then advance experiments. Host passes run
        concurrently, so a slow host (a worker upgrade can take minutes) never
        delays the others."""
        busy = self.db.query(
            "SELECT DISTINCT host_id FROM jobs WHERE state IN "
            "('queued', 'submitting', 'running', 'collecting')"
        )
        self.retain_leases()
        loop_time = asyncio.get_running_loop().time()
        for row in busy:
            host_id = row["host_id"]
            task = self._host_tasks.get(host_id)
            if task is not None and not task.done():
                continue
            # At most one pass per host per interval, however often we're woken.
            if (
                loop_time - self._host_last_pass.get(host_id, -1e9)
                < self.settings.scheduler_interval
            ):
                continue
            self._host_last_pass[host_id] = loop_time
            self._host_tasks[host_id] = asyncio.create_task(
                self._host_pass(host_id), name=f"newton-host-{host_id}"
            )
        leased = {lease["host_id"] for lease in self.router.leases()} if self.router else set()
        self.hosts.maintain_gpu(skip=leased)  # never under a timed run, nor while one waits
        self.hosts.recheck_down_hosts()  # hosts that went down come back by themselves
        self._retry_orphans()
        await self._advance_experiments()
        self.ticks += 1

    async def _host_pass(self, host_id: str) -> None:
        try:
            await self._submit(host_id)
            await self._poll(host_id)
            await self._collect(host_id)
        except Exception:
            log.exception("scheduler pass for host %s failed", host_id)

    # -- helpers --------------------------------------------------------------
    def _move(self, job: Row, dst: str, **fields: Any) -> bool:
        try:
            JOB.transition(self.db, job["id"], job["state"], dst, fields)
            job.update(state=dst, **fields)
            if dst in JOB.terminal:
                self.wake()  # experiments can advance now, not after the interval
            return True
        except (ConcurrentTransition, IllegalTransition) as e:
            log.info("skip transition for %s: %s", job["id"], e)
            return False

    def _retry_or_fail(self, job: Row, error: str) -> None:
        if job["attempt"] < self.settings.max_submit_attempts:
            delay = BACKOFF_BASE * 2 ** (job["attempt"] - 1)
            self._move(job, "queued", error=error, next_attempt_at=now() + delay)
        else:
            self._move(
                job,
                "failed",
                error=f"giving up after {job['attempt']} attempts: {error}",
                finished_at=now(),
            )

    def _set(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(
            f"UPDATE jobs SET {assignments} WHERE id = :_id",  # noqa: S608
            {**fields, "_id": job_id},
        )

    # -- submit ---------------------------------------------------------------
    async def _submit(self, host_id: str) -> None:
        t = now()
        # `submitting` rows: a crash mid-submit, or a submit retried after a transient
        # error (same remote id: the first try may be running there already). A cancel
        # asked meanwhile is acted on now, not after the backoff.
        rows = self.db.query(
            "SELECT * FROM jobs WHERE host_id = ? AND state IN ('submitting', 'queued') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ? "
            "OR (state = 'submitting' AND cancel_requested = 1)) ORDER BY created_at",
            (host_id, t),
        )
        for job in rows:
            if job["state"] == "submitting" and job["cancel_requested"]:
                await self._cancel_unstarted(job)  # never started once cancelled
                continue
            alone = _alone(job["manifest"])
            if job["state"] == "queued":
                if alone and self.hosts.upgrade_blocked(host_id):
                    # Its submit would only bounce: don't pause the host's inference for it.
                    self._note(job, "waiting for the host's worker upgrade")
                    continue
                if not self._has_capacity(job):
                    continue
                lease = self._device_lease(job)
                if lease is not None and not lease["granted"]:
                    continue
                attempt = job["attempt"] + 1
                with self.db.tx():  # the lease record goes with the move, or neither
                    moved = self._move(
                        job,
                        "submitting",
                        attempt=attempt,
                        remote_id=remote_job_id(job["id"], attempt),
                        next_attempt_at=None,
                    )
                    if moved and lease is not None:
                        record_event(self.db, "job", job["id"], "device_lease", lease)
                if not moved:
                    continue
            elif alone:
                lease = self._device_lease(job)  # after a restart, too: the host clears first
                if lease is not None and not lease["granted"]:
                    continue
            await self._submit_one(job)

    def _host_online(self, host_id: str) -> None:
        """The host is back: jobs waiting for it are submitted now, not after their
        backoff (which may be minutes by then), and jobs cancelled while it was down
        are stopped there."""
        for job_id in list(self._host_waits):
            row = self.db.query_one("SELECT host_id, state FROM jobs WHERE id = ?", (job_id,))
            if row is None or row["state"] not in ("queued", "submitting"):
                self._host_waits.pop(job_id, None)
            elif row["host_id"] == host_id:
                self._host_waits.pop(job_id, None)
                self._set(job_id, next_attempt_at=None)
        for job_id, (orphan_host, _) in list(self._orphans.items()):
            if orphan_host == host_id:
                self._orphan_due.pop(job_id, None)  # due now, whatever its backoff
        self._retry_orphans(host_id)
        self._host_last_pass.pop(host_id, None)
        self.wake()

    async def _cancel_unstarted(self, job: Row) -> None:
        """A cancel was asked while the job was still being submitted (often waiting
        for a host that is down). Never submit it again: ask the worker to stop it.
        If the worker has it, the poll confirms the cancel and collects what it left;
        if the worker never got it, it is cancelled here; if the host can't be
        reached, it is cancelled here all the same (remote state unknown), and the
        worker is asked to stop it when the host is back."""
        self._submit_failures.pop(job["id"], None)
        self._host_waits.pop(job["id"], None)
        try:
            runner = await self.hosts.runner(job["host_id"])
            status = await asyncio.wait_for(runner.cancel(job["remote_id"]), CANCEL_TIMEOUT)
        except RunnerError as e:
            if e.code == "http_404":  # no submit ever reached the worker
                self._move(job, "cancelled", error="cancelled before it started",
                           finished_at=now(), next_attempt_at=None)  # fmt: skip
                return
            self.hosts.note_reachability(job["host_id"], e)
            log.info("cancel of %s wasn't confirmed by its host: %s", job["id"], e)
            down = self.hosts.is_down(job["host_id"], e)
            note = UNSTARTED_CANCEL_UNKNOWN if down else f"{UNSTARTED_CANCEL_UNCONFIRMED} ({e})"
        except (TimeoutError, OSError) as e:
            log.info("cancel of %s wasn't confirmed by its host: %r", job["id"], e)
            note = UNSTARTED_CANCEL_UNCONFIRMED
        else:
            self.hosts.note_reachability(job["host_id"], None)
            self._move(job, "running", remote_status=dumps(status), error="cancel pending",
                       next_attempt_at=None)  # fmt: skip
            return
        with self.db.tx():  # the job and its pending remote stop, or neither
            moved = self._move(job, "cancelled", error=note[:500], finished_at=now(),
                               next_attempt_at=None)  # fmt: skip
            if moved:
                pending = {"host_id": job["host_id"], "remote_id": job["remote_id"]}
                record_event(self.db, "job", job["id"], "remote_cancel_pending", pending)
        if moved:
            self._orphans[job["id"]] = (job["host_id"], job["remote_id"])
            self._orphan_due.pop(job["id"], None)  # asked again on the next tick

    # -- remote stops for jobs cancelled before they started -------------------------
    def _load_orphans(self) -> None:
        """The remote stops still pending (asked before a restart, never confirmed)."""
        for row in self.db.query(
            "SELECT p.entity_id, p.data FROM events p WHERE p.entity_type = 'job' "
            "AND p.kind = 'remote_cancel_pending' AND NOT EXISTS (SELECT 1 FROM events d "
            "WHERE d.entity_type = 'job' AND d.entity_id = p.entity_id "
            "AND d.kind = 'remote_cancelled' AND d.id > p.id) ORDER BY p.id"
        ):
            data = loads(row["data"]) or {}
            if data.get("host_id") and data.get("remote_id"):
                self._orphans[row["entity_id"]] = (data["host_id"], data["remote_id"])

    def _retry_orphans(self, only_host: str | None = None) -> None:
        """Ask hosts that can be reached to stop the jobs cancelled before they started
        (once due). A host that is down is skipped: _host_online asks it once back."""
        t = time.monotonic()
        for job_id, (host_id, remote_id) in list(self._orphans.items()):
            if only_host is not None and host_id != only_host:
                continue
            task = self._orphan_tasks.get(job_id)
            due = self._orphan_due.get(job_id, (0, 0.0))[1]
            if (task is not None and not task.done()) or t < due:
                continue
            host = self.db.query_one("SELECT kind, status FROM hosts WHERE id = ?", (host_id,))
            if host is None or host["status"] == DELETED:  # nothing left to ask
                self._orphan_settled(job_id, remote_id, "host deleted")
                continue
            if host["kind"] != "local" and host["status"] != "online":
                continue
            task = asyncio.create_task(self._cancel_orphan(job_id, host_id, remote_id),
                                       name=f"newton-cancel-{job_id}")  # fmt: skip
            self._orphan_tasks[job_id] = task
            task.add_done_callback(functools.partial(self._orphan_task_done, job_id))

    def _orphan_task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._orphan_tasks.get(job_id) is task:
            self._orphan_tasks.pop(job_id, None)

    def _orphan_settled(self, job_id: str, remote_id: str, outcome: str) -> None:
        self._orphans.pop(job_id, None)
        self._orphan_due.pop(job_id, None)
        record_event(self.db, "job", job_id, "remote_cancelled",
                     {"remote_id": remote_id, "outcome": outcome})  # fmt: skip

    def _orphan_failed(self, job_id: str) -> None:
        failures = self._orphan_due.get(job_id, (0, 0.0))[0] + 1
        delay = min(ORPHAN_RETRY_START * 2 ** (failures - 1), ORPHAN_RETRY_MAX)
        self._orphan_due[job_id] = (failures, time.monotonic() + delay)

    async def _cancel_orphan(self, job_id: str, host_id: str, remote_id: str) -> None:
        try:
            runner = await self.hosts.runner(host_id)
            await asyncio.wait_for(runner.cancel(remote_id), CANCEL_TIMEOUT)
        except RunnerError as e:
            if e.code == "http_404":  # no submit had reached it: nothing to stop
                self._orphan_settled(job_id, remote_id, "never started")
                return
            self.hosts.note_reachability(host_id, e)
            self._orphan_failed(job_id)
            log.info("stopping cancelled job %s on its host failed: %s", job_id, e)
            return
        except (TimeoutError, OSError) as e:
            self._orphan_failed(job_id)
            log.info("stopping cancelled job %s on its host failed: %r", job_id, e)
            return
        self._orphan_settled(job_id, remote_id, "stopped")
        self.hosts.note_reachability(host_id, None)

    def _note(self, job: Row, note: str) -> None:
        if job["error"] != note:
            self._set(job["id"], error=note)

    def _has_capacity(self, job: Row) -> bool:
        host = self.db.query_one(
            "SELECT max_parallel_jobs FROM hosts WHERE id = ?", (job["host_id"],)
        )
        active = self.db.query(
            "SELECT manifest FROM jobs WHERE host_id = ? AND state IN "
            "('submitting', 'running', 'collecting')",
            (job["host_id"],),
        )
        if host is None or len(active) >= host["max_parallel_jobs"]:
            return False
        if self.hosts.gpu_busy(job["host_id"]):
            return False  # GPU support is changing the host's packages right now
        holder = self.router.lease_holder(job["host_id"]) if self.router else None
        if holder is not None and holder != job["id"]:
            return False  # a timed job is clearing the host: nothing starts before it
        # A GPU job, or any job timed for a speed verdict, gets the host to itself,
        # and nothing starts beside one: work running side by side skews the runtimes
        # that evidence compares (on a GB10 the CPU and GPU even share one memory bus).
        if _alone(job["manifest"]):
            if self._orphan_unasked(job["host_id"]):
                return False  # a job cancelled there may still run: ask it to stop first
            return not active
        return not any(_alone(a["manifest"]) for a in active)

    def _orphan_unasked(self, host_id: str) -> bool:
        """A job cancelled before it started on this host (it may be running there) whose
        stop hasn't been asked again yet, while the host can be asked (online). A timed
        job waits for that one ask (at most CANCEL_TIMEOUT), not for it to succeed."""
        if not any(h == host_id and self._orphan_due.get(j, (0, 0.0))[0] == 0
                   for j, (h, _) in self._orphans.items()):  # fmt: skip
            return False
        host = self.db.query_one("SELECT kind, status FROM hosts WHERE id = ?", (host_id,))
        return host is not None and (host["kind"] == "local" or host["status"] == "online")

    # -- device leases (SV3) ---------------------------------------------------------
    def _device_lease(self, job: Row) -> dict[str, Any] | None:
        """A timed job's host, cleared of inference routed by Newton first: the
        router stops sending requests there and lets the ones in flight finish
        (stopping stragglers after the drain timeout). None: no lease needed."""
        if self.router is None or not _alone(job["manifest"]):
            return None
        lease = self.router.lease(job["host_id"], job["id"])
        limit = f"{self.settings.lease_drain_timeout:.0f} s"
        if lease["waiting_on"] == "requests":
            self._note(
                job,
                f"waiting for {lease['in_flight']} model request(s) on this host "
                f"to finish: a timed job runs alone (stragglers are stopped after {limit})",
            )
        elif lease["waiting_on"] == "loading":
            self._note(
                job,
                f"waiting for {', '.join(lease['loading'])} to finish loading on "
                f"this host (at most {limit}): a timed job runs alone",
            )
        elif lease["waiting_on"] == "grace":
            self._note(job, "giving the engines a moment to stop the aborted requests")
        return lease

    def retain_leases(self) -> None:
        """Run every tick, for every host (not in host passes: a host whose last job
        just ended gets none). A lease lives while its job is submitting or running
        (after an agentd restart, a running timed job takes its host again before the
        router serves anything) and while it waits for the host to clear. It is given
        back during a retry backoff or an upgrade wait, and once timing is over
        (collecting), so inference never waits on a job that isn't running."""
        if self.router is None:
            return
        keep: dict[str, str] = {}
        t = now()
        for job in self.db.query(
            "SELECT id, host_id, state, manifest, next_attempt_at FROM jobs WHERE state IN "
            "('queued', 'submitting', 'running')"
        ):
            host_id = job["host_id"]
            if not _alone(job["manifest"]):
                continue
            if job["state"] != "queued":
                keep[host_id] = job["id"]
                if self.router.lease_holder(host_id) != job["id"]:  # agentd restarted
                    lease = self.router.lease(host_id, job["id"])
                    record_event(self.db, "job", job["id"], "device_lease",
                                 {**lease, "restarted": True})  # fmt: skip
            elif (
                self.router.lease_holder(host_id) == job["id"]
                and (job["next_attempt_at"] is None or job["next_attempt_at"] <= t)
                and not self.hosts.gpu_busy(host_id)  # it can't start meanwhile
                and not self.hosts.upgrade_blocked(host_id)
            ):
                keep[host_id] = job["id"]
        self.router.retain_leases(keep)

    def _lease_of(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT data FROM events WHERE entity_type = 'job' AND entity_id = ? "
            "AND kind = 'device_lease' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        data: dict[str, Any] | None = loads(row["data"]) if row else None
        return data

    def _lease_caveats(self, job: Row, results: dict[str, Any]) -> None:
        """What the lease couldn't guarantee for this attempt (requests aborted, a
        model still loading) discounts its timing, like a throttled GPU does."""
        caveats = (self._lease_of(job["id"]) or {}).get("caveats")
        if not caveats or not isinstance(results.get("environment"), dict):
            return
        env = results["environment"]
        env["timing_caveat"] = "; ".join([*filter(None, [env.get("timing_caveat")]), *caveats])

    async def _submit_one(self, job: Row) -> None:
        manifest = {**loads(job["manifest"]), "job_id": job["remote_id"]}
        try:
            bundle = Path(job["bundle_path"]).read_bytes()
            runner = await self.hosts.runner(job["host_id"])
            status = await runner.submit(manifest, bundle)
        except RunnerError as e:
            self.hosts.note_reachability(job["host_id"], e)
            log.warning("submit %s failed: %s", job["id"], e)
            if self.hosts.is_down(job["host_id"], e):
                # The host (or this Mac's network) is down: not a failed attempt. Wait
                # for it, same remote id (this try may have reached the worker), and
                # say so; a host back online retries at once (_host_online).
                waits = self._host_waits.get(job["id"], 0) + 1
                self._host_waits[job["id"]] = waits
                delay = min(HOST_WAIT_START * 2 ** (waits - 1), HOST_WAIT_MAX)
                self._set(job["id"], error=self.hosts.waiting_note(job["host_id"], e),
                          next_attempt_at=now() + delay)  # fmt: skip
                return
            if e.code == "worker_upgrade_pending":
                # Waiting for the host's worker upgrade is not a failed attempt.
                self._move(
                    job,
                    "queued",
                    attempt=job["attempt"] - 1,
                    error=str(e),
                    next_attempt_at=now() + UPGRADE_WAIT,
                )
                return
            if e.transient:
                # Retried as is (same remote id, still submitting, the host still held):
                # submit is idempotent, and this try may have started the job there.
                failures = self._submit_failures.get(job["id"], 0) + 1
                self._submit_failures[job["id"]] = failures
                if failures < self.settings.max_submit_attempts:
                    delay = BACKOFF_BASE * 2 ** (failures - 1)
                    self._set(job["id"], error=str(e), next_attempt_at=now() + delay)
                else:
                    self._submit_failures.pop(job["id"], None)
                    self._move(job, "failed", finished_at=now(),
                               error=f"giving up after {failures} attempts: {e}")  # fmt: skip
            else:
                self._move(job, "failed", error=str(e), finished_at=now())
            return
        except OSError as e:
            self._move(job, "failed", error=f"bundle unavailable: {e}", finished_at=now())
            return
        self._submit_failures.pop(job["id"], None)
        self._host_waits.pop(job["id"], None)
        self.hosts.note_reachability(job["host_id"], None)
        self._move(
            job,
            "running",
            remote_status=dumps(status),
            started_at=job["started_at"] or now(),
            error=None,
            next_attempt_at=None,
        )

    # -- poll -----------------------------------------------------------------
    async def _poll(self, host_id: str) -> None:
        rows = self.db.query(
            "SELECT * FROM jobs WHERE state = 'running' AND host_id = ?", (host_id,)
        )
        await asyncio.gather(*(self._poll_one(j) for j in rows))

    async def _poll_one(self, job: Row) -> None:
        try:
            runner = await self.hosts.runner(job["host_id"])
            if job["cancel_requested"]:
                await runner.cancel(job["remote_id"])
            status = await runner.status(job["remote_id"])
            await self._pull_logs(job, runner)
            self.hosts.note_reachability(job["host_id"], None)
        except RunnerError as e:
            self.hosts.note_reachability(job["host_id"], e)
            if e.code == "http_404":
                # The worker has no record of this attempt (e.g. its disk was wiped).
                if job["cancel_requested"]:
                    self._move(job, "cancelled", error="cancelled", finished_at=now())
                else:
                    self._retry_or_fail(job, f"worker lost the job: {e}")
            else:
                # Host trouble: keep monitoring (and retrying a requested cancel); a
                # job is only cancelled once the worker confirms it.
                prefix = "cancel pending" if job["cancel_requested"] else "monitoring"
                self._set(job["id"], error=f"{prefix}: {e}")
            return
        self._set(job["id"], remote_status=dumps(status))
        state = status.get("state")
        if state == "lost":
            self._retry_or_fail(job, status.get("error") or "job wrapper lost")
        elif state in WORKER_TERMINAL:
            self._move(job, "collecting", remote_status=dumps(status), error=None)

    def _stderr_tail(self, job_id: str, lines: int = 15) -> str:
        path = job_dir(self.settings, job_id) / "stderr.log"
        if not path.exists():
            return ""
        return "\n".join(path.read_text(errors="replace").strip().splitlines()[-lines:])

    async def _pull_logs(self, job: Row, runner: Any) -> None:
        offsets: dict[str, int] = loads(job["log_offsets"]) or {}
        d = job_dir(self.settings, job["id"])
        d.mkdir(parents=True, exist_ok=True)
        for stream in ("stdout", "stderr"):
            for _ in range(20):  # bounded catch-up per pass
                chunk = await runner.logs(job["remote_id"], stream, offsets.get(stream, 0))
                data = chunk.get("data", "")
                if not data:
                    break
                with open(d / f"{stream}.log", "a", encoding="utf-8") as f:
                    f.write(data)
                # Persist right after each write: a pass cancelled between chunks
                # (shutdown) must not re-append text on the next start.
                offsets[stream] = chunk["next_offset"]
                job["log_offsets"] = dumps(offsets)
                self._set(job["id"], log_offsets=job["log_offsets"])

    # -- collect --------------------------------------------------------------
    async def _collect(self, host_id: str) -> None:
        for job in self.db.query(
            "SELECT * FROM jobs WHERE state = 'collecting' AND host_id = ?", (host_id,)
        ):
            await self._collect_one(job)

    async def _collect_one(self, job: Row) -> None:
        remote = loads(job["remote_status"]) or {}
        try:
            runner = await self.hosts.runner(job["host_id"])
            await self._pull_logs(job, runner)
            blob = await runner.artifacts(job["remote_id"])
        except RunnerError as e:
            if e.code != "http_404":
                # Host trouble (unreachable, upgrade pending, ...): the artifacts are
                # still on the worker. Never finalize a job because its host is down.
                self._set(job["id"], error=f"collecting: {e}")
                return
            blob = b""  # the worker has no record of this job; nothing to collect
        adir = Path(job["artifacts_dir"])
        files: list[str] = []
        error = remote.get("error")
        try:
            if blob:
                files = extract_tar_gz(blob, adir)
        except (UnsafeArchive, OSError, ValueError) as e:
            error = f"rejected artifacts: {e}"

        final = FINAL_FROM_WORKER.get(remote.get("state", ""), "failed")
        if job["cancel_requested"] and final != "succeeded":
            final = "cancelled"
        results = None
        metrics = None
        results_path = adir / "results.json"
        if results_path.is_file():
            try:
                results = json.loads(results_path.read_text())
                metrics = results.get("metrics")
            except (json.JSONDecodeError, OSError) as e:
                error = f"unreadable results.json: {e}"
        if results and results.get("error"):
            error = results["error"]  # the benchmark said exactly what went wrong
        if results is not None:
            self._lease_caveats(job, results)
        if final == "failed":
            tail = self._stderr_tail(job["id"])
            if tail and (not error or tail not in error):
                error = f"{error or 'failed'}\n{tail}"
        if (
            final == "succeeded"
            and "results.json" in loads(job["manifest"])["artifact_paths"]
            and results is None
        ):
            final, error = "failed", error or "job produced no results.json"
        self._move(
            job,
            final,
            results=dumps(results) if results is not None else None,
            metrics=dumps(metrics) if metrics is not None else None,
            error=error,
            finished_at=now(),
        )
        record_event(self.db, "job", job["id"], "artifacts", {"files": files[:100]})

    # -- experiments ----------------------------------------------------------
    async def _advance_experiments(self) -> None:
        for exp in self.db.query(
            "SELECT * FROM experiments WHERE state IN ('executing', 'evaluating')"
        ):
            jobs = self.db.query("SELECT * FROM jobs WHERE experiment_id = ?", (exp["id"],))
            if exp["state"] == "executing":
                if not all(j["state"] in JOB.terminal for j in jobs):
                    continue
                try:
                    EXPERIMENT.transition(self.db, exp["id"], "executing", "evaluating")
                except ConcurrentTransition:
                    continue
            try:
                self._evaluate(exp, jobs)
            except Exception as e:
                log.exception("evaluation of %s failed", exp["id"])
                EXPERIMENT.transition(
                    self.db,
                    exp["id"],
                    "evaluating",
                    "failed",
                    {"error": f"{type(e).__name__}: {e}"},
                )

    def _evaluate(self, exp: Row, jobs: list[Row]) -> None:
        host = self.db.query_one("SELECT * FROM hosts WHERE id = ?", (exp["host_id"],))
        hw = loads(host["hardware"]) if host and host["hardware"] else {}
        manifests = [loads(j["manifest"]) for j in jobs]
        provenance = {
            "repository_commit": next(
                (m.get("repository_commit") for m in manifests if m.get("repository_commit")),
                repository_commit(self.settings.resources_dir),
            ),
            "worker_version": (hw.get("worker") or {}).get("version"),
            "host_id": exp["host_id"],
            "generated_at": now(),
            "jobs": {
                j["label"]: {
                    "id": j["id"],
                    "attempt": j["attempt"],
                    "remote_id": j["remote_id"],
                    "device_lease": self._lease_of(j["id"]),
                }
                for j in jobs
            },
        }
        report = evaluate(exp, jobs, provenance)
        path = write_report(report, exp, jobs, host, self.settings.reports_dir / exp["id"])
        report.report_path = str(path)
        EXPERIMENT.transition(
            self.db,
            exp["id"],
            "evaluating",
            "reported",
            {
                "evaluation": report.model_dump_json(),
                "evidence": report.evidence,
                "report_path": str(path),
            },
            {"evidence": report.evidence},
        )
