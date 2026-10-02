"""Host lifecycle, placement freshness, scheduling and evidence rules from the
G2/G3 review (each was found broken or untested by mutation testing)."""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from conftest import AUTH, FakeRemote, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.contracts import ExperimentSpec, ValidationReport, VariantEvaluation
from newton_agentd.orchestration.jobs import create_job, selftest_manifest
from newton_agentd.reporting.markdown import write_report
from newton_agentd.research.evaluation import evaluate
from newton_agentd.runners.bundle import build_bundle
from newton_agentd.storage.db import dumps
from test_placement import GB10, MAC, set_hardware
from test_scheduler_resilience import StubRunner

SMALL = {"resolutions": [32, 64], "repeats": 1}
METRICS = {"l2_error": 1e-3, "observed_order": 1.0, "runtime": 1.0, "conservation": 0.0,
           "max_overshoot": 0.0, "stable": True}  # fmt: skip


def spec(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "t",
        "variants": [
            {"role": "baseline", "label": "b", "params": {"scheme": "upwind", **SMALL}},
            {"role": "candidate", "label": "c", "params": {"scheme": "lax_wendroff", **SMALL}},
        ],
        **overrides,
    }


def stub_host(client: TestClient, hardware: dict[str, Any]) -> StubRunner:
    host = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"}).json()
    set_hardware(client, host["id"], hardware, "online")
    stub = StubRunner(host["id"])
    hosts = client.app.state.ctx.hosts  # type: ignore[attr-defined]
    original = hosts.runner

    async def pick(host_id: str) -> Any:
        return stub if host_id == stub.host_id else await original(host_id)

    hosts.runner = pick
    return stub


# -- hosts -------------------------------------------------------------------


def test_a_deleted_host_stays_deleted(client: TestClient) -> None:
    host = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"}).json()
    assert client.delete(f"/hosts/{host['id']}").status_code == 204
    for method, path in (
        ("get", f"/hosts/{host['id']}"),
        ("post", f"/hosts/{host['id']}/check"),
        ("post", f"/hosts/{host['id']}/connect"),
        ("post", f"/hosts/{host['id']}/bootstrap"),
    ):
        assert getattr(client, method)(path).status_code == 404, path
    r = client.post("/experiments", json=spec(host_id=host["id"], backend="cpu"))
    assert r.status_code == 404
    assert host["id"] not in [h["id"] for h in client.get("/hosts").json()]


def test_two_installs_on_one_account_both_work(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    from test_bootstrap_api import add_trusted_host

    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        a = add_trusted_host(client, sys.executable, use_venv=False, install_deps=False)
        b_body = {"name": "spark-b", "ssh_target": "spark", "ssh_user": "tester",
                  "python": sys.executable, "use_venv": False, "install_deps": False}  # fmt: skip
        b = client.post("/hosts", json=b_body).json()
        assert client.post(f"/hosts/{a['id']}/connect").status_code == 200
        assert client.post(f"/hosts/{b['id']}/connect").status_code == 200
        # a's token must still be accepted after b installed its own
        assert client.post(f"/hosts/{a['id']}/check").json()["status"] == "online"
        assert len(list((fake_remote.fp / "tokens").iterdir())) == 2


def test_auto_placement_refreshes_a_stale_online_host(client: TestClient) -> None:
    set_hardware(client, "local", MAC, "online")
    host = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"}).json()
    set_hardware(client, host["id"], GB10, "online")
    client.app.state.ctx.db.execute(  # type: ignore[attr-defined]
        "UPDATE hosts SET last_checked_at = ? WHERE id = ?", (time.time() - 3600, host["id"])
    )
    exp = client.post("/experiments", json=spec()).json()  # spark can't be reached now
    assert exp["host_id"] == "local"
    assert client.get(f"/hosts/{host['id']}").json()["status"].startswith("error:")


def test_a_never_probed_host_is_checked_before_placement(client: TestClient) -> None:
    set_hardware(client, "local", MAC, "online")
    host = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"}).json()
    hosts = client.app.state.ctx.hosts  # type: ignore[attr-defined]
    probed: list[str] = []

    async def fake_check(host_id: str) -> dict[str, Any]:
        probed.append(host_id)
        set_hardware(client, host_id, GB10, "online")
        return dict(hosts.get(host_id))

    hosts.check = fake_check
    exp = client.post("/experiments", json=spec(host_id=host["id"])).json()
    assert probed == [host["id"]]
    assert exp["spec"]["backend"] == "cuda"


# -- scheduling --------------------------------------------------------------


def add_job(client: TestClient, host_id: str, backend: str, state: str = "queued") -> str:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    job_id = create_job(
        ctx.db, ctx.settings, host_id=host_id, role="selftest", label=f"j-{backend}-{state}",
        manifest={**selftest_manifest(), "backend": backend}, bundle=build_bundle(None, {}),
        state=state,
    )  # fmt: skip
    if state == "running":
        ctx.db.execute("UPDATE jobs SET attempt = 1, remote_id = 'r' WHERE id = ?", (job_id,))
    ctx.scheduler.wake()
    return job_id


def test_nothing_starts_beside_a_gpu_job(client: TestClient) -> None:
    stub = stub_host(client, GB10)
    stub.block_submit.set()
    add_job(client, stub.host_id, "gpu", "running")  # legacy alias for cuda, stored long ago
    cpu = add_job(client, stub.host_id, "cpu")
    gpu = add_job(client, stub.host_id, "cuda")
    time.sleep(1.0)
    assert client.get(f"/jobs/{cpu}").json()["state"] == "queued"
    assert client.get(f"/jobs/{gpu}").json()["state"] == "queued"


def test_polling_stays_bounded_however_often_we_are_woken(client: TestClient) -> None:
    stub = stub_host(client, GB10)
    calls = {"n": 0}
    status = stub.status

    async def counting(remote_id: str) -> dict[str, Any]:
        calls["n"] += 1
        return await status(remote_id)

    stub.status = counting  # type: ignore[method-assign]
    add_job(client, stub.host_id, "cpu", "running")
    scheduler = client.app.state.ctx.scheduler  # type: ignore[attr-defined]
    deadline = time.time() + 1.5
    while time.time() < deadline:
        scheduler.wake()
        time.sleep(0.001)
    assert calls["n"] <= 30, calls  # interval is 0.1 s


def test_force_cancel_asks_the_worker_first(client: TestClient) -> None:
    stub = stub_host(client, GB10)
    cancels: list[str] = []
    cancel = stub.cancel

    async def recording(remote_id: str) -> dict[str, Any]:
        cancels.append(remote_id)
        return await cancel(remote_id)

    stub.cancel = recording  # type: ignore[method-assign]
    job = add_job(client, stub.host_id, "cuda", "running")
    out = client.post(f"/jobs/{job}/cancel?force=true").json()
    assert out["state"] == "cancelled"
    assert cancels == ["r"] and "worker confirmed" in out["error"]


def test_failed_job_carries_the_benchmarks_own_error(client: TestClient) -> None:
    stub = stub_host(client, GB10)

    async def artifacts(remote_id: str) -> bytes:
        return build_bundle(None, {"results.json": {"error": "cuda backend unusable: NVRTC"}})

    stub.artifacts = artifacts  # type: ignore[method-assign]
    stub.state = "failed"
    job = add_job(client, stub.host_id, "cuda", "running")
    done = wait_for(
        lambda: client.get(f"/jobs/{job}").json(), lambda j: j["state"] == "failed", timeout=30
    )
    assert done["error"].startswith("cuda backend unusable: NVRTC")


# -- evidence ----------------------------------------------------------------


def evaluated(backends: tuple[tuple[str, str], tuple[str, str]], expected: str) -> Any:
    spec_ = ExperimentSpec.model_validate({**spec(host_id="h", backend=expected)})

    def job(label: str, backend: str, device: str) -> dict[str, Any]:
        results = {"backend": backend, "environment": {"device": device}}
        return {"id": f"job-{label}", "label": label, "state": "succeeded",
                "metrics": dumps(METRICS), "results": dumps(results)}  # fmt: skip

    exp = {"id": "e", "title": "t", "spec": dumps(spec_.model_dump())}
    (bb, bd), (cb, cd) = backends
    return evaluate(exp, [job("b", bb, bd), job("c", cb, cd)], {})


def test_backend_evidence_rules() -> None:
    gb10 = ("cuda", "NVIDIA GB10")
    baseline_wrong = evaluated((("cpu", "cpu (arm)"), gb10), "cuda").verdicts[0]
    assert baseline_wrong.evidence == "unknown" and "baseline ran on cpu" in baseline_wrong.summary
    fake_gpu = evaluated((gb10, ("cuda", "cpu (arm)")), "cuda").verdicts[0]
    assert fake_gpu.evidence == "red"
    legacy = ExperimentSpec.model_validate(spec(host_id="h", backend="gpu"))
    assert legacy.backend == "cuda"


def test_report_shows_where_each_variant_ran(tmp_path: Any) -> None:
    report = ValidationReport(
        experiment_id="e", title="t", benchmark="linear_advection_1d", evidence="green",
        summary="s", verdicts=[], provenance={},
        variants=[VariantEvaluation(label="b", role="baseline", job_id="j", job_state="succeeded",
                                    backend="cuda", device="NVIDIA GB10")],
    )  # fmt: skip
    stored = ExperimentSpec.model_validate(spec(host_id="h")).model_dump()
    exp = {"id": "e", "title": "t", "host_id": "h", "created_at": 0, "spec": dumps(stored)}
    path = write_report(report, exp, [], {"name": "spark", "hardware": json.dumps(GB10)}, tmp_path)
    assert "cuda: NVIDIA GB10" in path.read_text()


def test_throttled_timings_make_cost_inconclusive() -> None:
    spec_ = ExperimentSpec.model_validate(spec(host_id="h", backend="cpu"))
    better = {**METRICS, "l2_error": 1e-5, "observed_order": 2.0}

    def job(label: str, metrics: dict[str, Any], caveat: str | None) -> dict[str, Any]:
        env = {"device": "cpu (arm)", **({"timing_caveat": caveat} if caveat else {})}
        results = {"backend": "cpu", "environment": env}
        return {"id": f"job-{label}", "label": label, "state": "succeeded",
                "metrics": dumps(metrics), "results": dumps(results)}  # fmt: skip

    exp = {"id": "e", "title": "t", "spec": dumps(spec_.model_dump())}
    clean = evaluate(exp, [job("b", METRICS, None), job("c", better, None)], {})
    assert clean.verdicts[0].evidence == "green"
    throttled = evaluate(
        exp, [job("b", METRICS, None), job("c", better, "Low Power Mode was on")], {}
    )
    verdict = throttled.verdicts[0]
    cost = next(c for c in verdict.checks if c.name == "cost")
    assert verdict.evidence == "yellow" and cost.passed is None and "Low Power" in cost.detail
