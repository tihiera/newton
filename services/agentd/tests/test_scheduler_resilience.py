"""Scheduler behaviour when a host misbehaves: other hosts keep going, results
are never thrown away because of host trouble, and cancels wait for the worker."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from conftest import wait_for, wait_job
from fastapi.testclient import TestClient
from newton_agentd.orchestration.jobs import create_job, selftest_manifest
from newton_agentd.runners.base import RunnerError
from newton_agentd.runners.bundle import build_bundle
from newton_agentd.storage.db import dumps

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}


class StubRunner:
    """A worker host whose behaviour each test scripts."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self.block_submit = asyncio.Event()  # never set: submit hangs (a slow upgrade)
        self.artifacts_error: RunnerError | None = None
        self.cancel_error: RunnerError | None = None
        self.state = "running"

    async def ensure_ready(self) -> None:
        pass

    async def submit(self, manifest: dict[str, Any], bundle: bytes) -> dict[str, Any]:
        await self.block_submit.wait()
        return {"state": "running"}

    async def status(self, remote_id: str) -> dict[str, Any]:
        return {"job_id": remote_id, "state": self.state}

    async def logs(self, remote_id: str, stream: str, offset: int) -> dict[str, Any]:
        return {"data": "", "next_offset": offset}

    async def artifacts(self, remote_id: str) -> bytes:
        if self.artifacts_error is not None:
            raise self.artifacts_error
        return build_bundle(None, {"results.json": {"metrics": {"checksum": 1}}})

    async def cancel(self, remote_id: str) -> dict[str, Any]:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.state = "cancelled"
        return {"state": "cancelled"}

    async def health(self) -> dict[str, Any]:
        return {"ok": True}

    async def hardware(self) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        pass


@pytest.fixture
def stub(client: TestClient) -> StubRunner:
    r = client.post("/hosts", json={"name": "flaky", "ssh_target": "flaky"})
    runner = StubRunner(r.json()["id"])
    hosts = client.app.state.ctx.hosts  # type: ignore[attr-defined]
    original = hosts.runner

    async def pick(host_id: str) -> Any:
        return runner if host_id == runner.host_id else await original(host_id)

    hosts.runner = pick
    return runner


def add_job(client: TestClient, host_id: str, state: str, **fields: Any) -> str:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    job_id = create_job(
        ctx.db,
        ctx.settings,
        host_id=host_id,
        role="selftest",
        label="selftest",
        manifest=selftest_manifest(),
        bundle=build_bundle(None, {}),
        state=state,
    )
    if fields:
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        ctx.db.execute(f"UPDATE jobs SET {assignments} WHERE id = :id", {**fields, "id": job_id})  # noqa: S608
    ctx.scheduler.wake()
    return job_id


def test_slow_host_does_not_block_local_jobs(client: TestClient, stub: StubRunner) -> None:
    stuck = add_job(client, stub.host_id, "queued")
    local = client.post("/hosts/local/selftest", json={}).json()
    assert wait_job(client, local["id"], TERMINAL, timeout=60)["state"] == "succeeded"
    assert client.get(f"/jobs/{stuck}").json()["state"] == "submitting"


def test_host_trouble_while_collecting_keeps_the_job(client: TestClient, stub: StubRunner) -> None:
    stub.artifacts_error = RunnerError("upgrade failed", code="deps_missing", transient=False)
    job_id = add_job(
        client,
        stub.host_id,
        "collecting",
        attempt=1,
        remote_id="r-1",
        remote_status=dumps({"state": "succeeded"}),
    )
    wait_for(
        lambda: client.get(f"/jobs/{job_id}").json(),
        lambda j: (j["error"] or "").startswith("collecting:"),
        timeout=30,
    )
    assert client.get(f"/jobs/{job_id}").json()["state"] == "collecting"
    stub.artifacts_error = None  # the host recovers
    done = wait_job(client, job_id, TERMINAL, timeout=30)
    assert done["state"] == "succeeded"
    assert done["metrics"] == {"checksum": 1}


def test_cancel_waits_for_the_worker(client: TestClient, stub: StubRunner) -> None:
    stub.cancel_error = RunnerError("host down", code="deps_missing", transient=False)
    job_id = add_job(client, stub.host_id, "running", attempt=1, remote_id="r-1")
    client.post(f"/jobs/{job_id}/cancel")
    wait_for(
        lambda: client.get(f"/jobs/{job_id}").json(),
        lambda j: (j["error"] or "").startswith("cancel pending:"),
        timeout=30,
    )
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"  # GPU still busy
    stub.cancel_error = None
    assert wait_job(client, job_id, TERMINAL, timeout=30)["state"] == "cancelled"


def test_polling_respects_the_interval(client: TestClient, stub: StubRunner) -> None:
    calls = {"n": 0}
    original_status = stub.status

    async def counting_status(remote_id: str) -> dict[str, Any]:
        calls["n"] += 1
        return await original_status(remote_id)

    stub.status = counting_status  # type: ignore[method-assign]
    add_job(client, stub.host_id, "running", attempt=1, remote_id="r-1")
    time.sleep(2.0)
    # interval is 0.1 s in tests: ~20 polls, never the hundreds/s of a busy loop
    assert 5 <= calls["n"] <= 40, calls
