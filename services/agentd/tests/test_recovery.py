"""B2 acceptance: kill agentd while a job runs, restart, and it resumes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from conftest import TOKEN, wait_for


@contextmanager
def agentd(data_dir: Path) -> Iterator[tuple[subprocess.Popen[str], httpx.Client]]:
    env = {
        **os.environ,
        "NEWTON_API_TOKEN": TOKEN,
        "NEWTON_SECRET_BACKEND": "memory",
        "NEWTON_SCHEDULER_INTERVAL": "0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "newton_agentd.main", "serve", "--port", "0",
         "--data-dir", str(data_dir), "--log-level", "warning"],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )  # fmt: skip
    assert proc.stdout is not None
    ready = json.loads(proc.stdout.readline())
    assert ready["event"] == "ready"
    client = httpx.Client(
        base_url=ready["url"], headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30
    )
    try:
        yield proc, client
    finally:
        client.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=30)


def job(client: httpx.Client, job_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/jobs/{job_id}").json()
    return body


def test_resume_monitoring_after_hard_kill(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with agentd(data) as (proc, client):
        created = client.post("/hosts/local/selftest", json={"sleep": 4}).json()
        wait_for(lambda: job(client, created["id"]), lambda j: j["state"] == "running", 30)
        proc.send_signal(signal.SIGKILL)  # no graceful shutdown at all
        proc.wait(timeout=10)

    with agentd(data) as (_, client):
        # Still `running` in the DB; the new process picks it up and completes it.
        done = wait_for(
            lambda: job(client, created["id"]),
            lambda j: j["state"] in ("succeeded", "failed", "cancelled", "timed_out"),
            60,
        )
        assert done["state"] == "succeeded", done
        assert done["attempt"] == 1  # resumed, not re-run
        logs = client.get(f"/jobs/{created['id']}/logs").json()
        assert "selftest done" in logs["data"]
