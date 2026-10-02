"""Job records: creation, views, local log access, cancellation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..contracts import JobManifest
from ..storage.db import Database, Row, dumps, loads, new_id, now
from .state_machine import JOB, record_event

SELFTEST_MANIFEST_ARTIFACTS = ["results.json", "artifacts/"]


class JobNotFound(KeyError):
    pass


def job_view(row: Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("manifest", "remote_status", "metrics", "results", "log_offsets"):
        out[key] = loads(row[key])
    out.pop("bundle_path", None)
    return out


def remote_job_id(job_id: str, attempt: int) -> str:
    """Worker-side id: unique per attempt so retries never collide."""
    return f"{job_id}-a{attempt}"


def job_dir(settings: Settings, job_id: str) -> Path:
    return settings.artifacts_dir / job_id


def create_job(
    db: Database,
    settings: Settings,
    *,
    host_id: str,
    role: str,
    label: str,
    manifest: dict[str, Any],
    bundle: bytes,
    experiment_id: str | None = None,
    state: str = "queued",
    job_id: str | None = None,
) -> str:
    job_id = job_id or new_id("job")
    manifest = {**manifest, "job_id": job_id, "experiment_id": experiment_id}
    JobManifest.model_validate(manifest)
    d = job_dir(settings, job_id)
    d.mkdir(parents=True, exist_ok=True)
    bundle_path = d / "bundle.tar.gz"
    bundle_path.write_bytes(bundle)
    t = now()
    with db.tx():
        db.insert(
            "jobs",
            {
                "id": job_id,
                "experiment_id": experiment_id,
                "host_id": host_id,
                "role": role,
                "label": label,
                "manifest": dumps(manifest),
                "bundle_path": str(bundle_path),
                "state": state,
                "artifacts_dir": str(d / "artifacts"),
                "created_at": t,
                "updated_at": t,
            },
        )
        record_event(db, "job", job_id, "created", {"state": state, "host_id": host_id})
    return job_id


def selftest_manifest(sleep: float = 0.0, fail: bool = False) -> dict[str, Any]:
    command = ["python", "-m", "newton_worker.selftest"]
    if sleep:
        command += ["--sleep", f"{float(sleep):g}"]
    if fail:
        command.append("--fail")
    return {
        "job_id": "placeholder",
        "backend": "cpu",
        "benchmark": "selftest",
        "command": command,
        "timeout_seconds": 300,
        "metrics": ["checksum", "runtime"],
        "artifact_paths": SELFTEST_MANIFEST_ARTIFACTS,
    }


def get_row(db: Database, job_id: str) -> Row:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise JobNotFound(job_id)
    return row


def list_jobs(
    db: Database, *, state: str | None = None, experiment_id: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if experiment_id:
        clauses.append("experiment_id = ?")
        params.append(experiment_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(
        f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
        (*params, limit),
    )
    return [job_view(r) for r in rows]


def read_log(
    settings: Settings, job_id: str, stream: str, offset: int, limit: int
) -> dict[str, Any]:
    if stream not in ("stdout", "stderr"):
        raise ValueError("stream must be stdout or stderr")
    path = job_dir(settings, job_id) / f"{stream}.log"
    size = path.stat().st_size if path.exists() else 0
    offset = max(0, min(offset, size))
    data = b""
    if path.exists():
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(max(1, min(limit, 1024 * 1024)))
    return {
        "job_id": job_id,
        "stream": stream,
        "offset": offset,
        "next_offset": offset + len(data),
        "size": size,
        "data": data.decode("utf-8", errors="replace"),
    }


def request_cancel(db: Database, job_id: str) -> dict[str, Any]:
    """Cancel immediately if not yet on a worker; otherwise flag it so the
    scheduler asks the worker to stop it."""
    row = get_row(db, job_id)
    state = row["state"]
    if state in JOB.terminal:
        return job_view(row)
    if state in ("pending_approval", "queued"):
        JOB.transition(db, job_id, state, "cancelled", {"finished_at": now(), "error": "cancelled"})
    else:
        with db.tx():
            db.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                (now(), job_id),
            )
            record_event(db, "job", job_id, "cancel_requested", {})
    return job_view(get_row(db, job_id))


def force_cancel(db: Database, job_id: str, reason: str) -> dict[str, Any]:
    """The way out when a job's host is gone for good: mark it cancelled without
    the worker's confirmation (its remote state is unknown, and that's recorded)."""
    row = get_row(db, job_id)
    if row["state"] in JOB.terminal:
        return job_view(row)
    db.execute(
        "UPDATE jobs SET state = 'cancelled', error = ?, finished_at = ?, updated_at = ? "
        "WHERE id = ?",
        (f"force-cancelled: {reason}", now(), now(), job_id),
    )
    record_event(
        db,
        "job",
        job_id,
        "state",
        {"from": row["state"], "to": "cancelled", "forced": True, "reason": reason},
    )
    return job_view(get_row(db, job_id))
