"""P1 through agentd: 2D and throughput specs, the runtime estimate in the
approval, speed evidence, and the rule that only approved kernel code counts."""

from __future__ import annotations

import importlib.util
import json
from typing import Any

import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd.contracts import ExperimentSpec, VariantEvaluation
from newton_agentd.orchestration.capabilities import PlacementError, place
from newton_agentd.research.estimate import estimate
from newton_agentd.research.evaluation import evaluate, integrity_problem, judge_performance
from newton_agentd.storage.db import dumps, new_id
from pydantic import ValidationError
from test_placement import GB10, MAC, host, set_hardware

HAS_MLX = importlib.util.find_spec("mlx") is not None


def perf_spec(**overrides: Any) -> dict[str, Any]:
    common = {"mode": "throughput", "resolutions": [256], "steps": 10, "precision": "float32",
              "repeats": 1}  # fmt: skip
    return {
        "title": "kernel vs array",
        "benchmark": "linear_advection_2d",
        "objective": "performance",
        "variants": [
            {"role": "baseline", "label": "array",
             "params": {"scheme": "muscl_vanleer", "implementation": "array", **common}},
            {"role": "candidate", "label": "kernel",
             "params": {"scheme": "muscl_vanleer", "implementation": "kernel", **common}},
        ],
        **overrides,
    }  # fmt: skip


# -- contracts ------------------------------------------------------------------


def test_speed_comparisons_must_be_fair() -> None:
    spec = ExperimentSpec.model_validate(perf_spec())
    assert spec.dims == 2 and spec.needs_gpu and spec.max_cells == 256 * 256
    unfair = perf_spec()
    unfair["variants"][1]["params"]["steps"] = 20
    with pytest.raises(ValidationError, match="differ in steps"):
        ExperimentSpec.model_validate(unfair)
    for field, value in (("scheme", "upwind"), ("repeats", 5), ("initial_condition", "square")):
        changed = perf_spec()
        changed["variants"][1]["params"][field] = value  # "faster" must mean "same result"
        with pytest.raises(ValidationError, match=f"differ in {field}"):
            ExperimentSpec.model_validate(changed)
    convergence = perf_spec()
    convergence["variants"][1]["params"]["mode"] = "convergence"
    with pytest.raises(ValidationError, match="compares throughput runs"):
        ExperimentSpec.model_validate(convergence)
    with pytest.raises(ValidationError, match="exactly one resolution"):
        ExperimentSpec.model_validate(
            perf_spec(variants=[{"role": "baseline", "label": "a", "params": {
                "scheme": "upwind", "mode": "throughput", "resolutions": [64, 128]}}] * 2)
        )  # fmt: skip
    with pytest.raises(ValidationError, match="needs a GPU backend"):
        ExperimentSpec.model_validate(perf_spec(backend="cpu"))
    too_big = perf_spec()
    for v in too_big["variants"]:
        v["params"]["resolutions"] = [32768]
    with pytest.raises(ValidationError, match="at most 16384"):
        ExperimentSpec.model_validate(too_big)


def test_kernels_are_never_placed_on_a_cpu() -> None:
    mac = {**MAC, "packages": {"mlx": "0.32.3"}}
    hosts = [host("local", "local", "online", mac), host("spark", "ssh", "online", GB10)]
    # Small grid: "auto" would keep array code on this Mac's CPU, kernels go to a GPU.
    assert place(hosts, "auto", "auto", False, 4096, needs_gpu=True)[1] == "cuda"
    hosts[1]["status"] = "error:unreachable"
    assert place(hosts, "auto", "auto", False, 4096, needs_gpu=True)[1] == "metal"
    no_gpu = [host("local", "local", "online", {**MAC, "apple_gpu": None})]
    with pytest.raises(PlacementError):
        place(no_gpu, "auto", "auto", False, 4096, needs_gpu=True)


# -- the runtime estimate -----------------------------------------------------------


def add_history(client: TestClient, backend: str, params: dict[str, Any], cells: int,
                rate: float, caveat: str | None = None) -> None:  # fmt: skip
    results = {"backend": backend, "params": params,
               "environment": {"timing_caveat": caveat} if caveat else {},
               "runs": [{"cells": cells, "cell_updates_per_s": rate}]}  # fmt: skip
    db = client.app.state.ctx.db  # type: ignore[attr-defined]
    db.execute(
        "INSERT INTO jobs (id, host_id, role, label, manifest, state, attempt, results, "
        "artifacts_dir, log_offsets, cancel_requested, created_at, updated_at, finished_at) "
        "VALUES (?, 'local', 'candidate', 'k', '{}', 'succeeded', 1, ?, '', '{}', 0, 0, 0, 0)",
        (new_id("job"), json.dumps(results)),
    )


def test_estimate_uses_comparable_history_only(client: TestClient) -> None:
    db = client.app.state.ctx.db  # type: ignore[attr-defined]
    spec = ExperimentSpec.model_validate(perf_spec())  # 2D, 256^2, float32
    rough = estimate(db, spec, "local", "metal")
    assert rough["basis"] == "rough default rates" and rough["seconds"] > 0
    kernel = {"implementation": "kernel", "mode": "throughput", "dims": 2, "precision": "float32"}
    # Not comparable: a 1D run, a tiny grid, another precision, a throttled run.
    add_history(client, "metal", {**kernel, "dims": 1}, 256 * 256, 1e3)
    add_history(client, "metal", kernel, 64, 1e3)
    add_history(client, "metal", {**kernel, "precision": "float64"}, 256 * 256, 1e3)
    add_history(client, "metal", kernel, 256 * 256, 1e3, caveat="Low Power Mode was on")
    assert estimate(db, spec, "local", "metal") == rough
    add_history(client, "metal", kernel, 512 * 512, 1e3)  # comparable: 4x the cells
    slow = estimate(db, spec, "local", "metal")
    assert slow["basis"] == "partly measured on this host" and slow["seconds"] > 1000


def test_refusal_is_per_job_and_memory_is_checked(client: TestClient) -> None:
    set_hardware(client, "local", {**MAC, "memory": {"total": 36 * 2**30}}, "online")
    common = {"mode": "throughput", "resolutions": [512], "steps": 2000, "repeats": 1}
    many = {
        "title": "many", "benchmark": "linear_advection_2d", "objective": "performance",
        "backend": "cpu", "timeout_seconds": 300,
        "variants": [{"role": "baseline" if i == 0 else "candidate", "label": f"v{i}",
                      "params": {"scheme": "upwind", **common}} for i in range(8)],
    }  # fmt: skip
    r = client.post("/experiments", json=many)  # each job fits its timeout; all 8 don't
    assert r.status_code == 201, r.text
    huge = {**many, "timeout_seconds": 24 * 3600, "variants": [
        {"role": role, "label": role, "params": {**common, "scheme": "upwind",
                                                 "resolutions": [16384], "steps": 1}}
        for role in ("baseline", "candidate")]}  # fmt: skip
    r = client.post("/experiments", json=huge)
    assert r.status_code == 400 and "GB of memory" in r.text, r.text


def test_impossible_runs_are_refused_up_front(client: TestClient) -> None:
    set_hardware(client, "local", {**MAC, "packages": {"mlx": "0.32.3"}}, "online")
    huge = {
        "title": "too big", "benchmark": "linear_advection_2d", "backend": "cpu",
        "timeout_seconds": 60,
        "variants": [
            {"role": "baseline", "label": "a",
             "params": {"scheme": "upwind", "resolutions": [8192]}},
            {"role": "candidate", "label": "b",
             "params": {"scheme": "lax_wendroff", "resolutions": [8192]}},
        ],
    }  # fmt: skip
    r = client.post("/experiments", json=huge)
    assert r.status_code == 400 and "far over its 60 s timeout" in r.text


# -- evidence ------------------------------------------------------------------------

FAST = {"stable": True, "conservation": 1e-9, "agrees_with_numpy": True,
        "reference_agreement": 1e-7, "time_per_step": 1e-3, "achieved_gbps": 70.0,
        "bandwidth_fraction": 0.3}  # fmt: skip


def test_performance_evidence() -> None:
    slow = {**FAST, "time_per_step": 5e-3}
    assert judge_performance(slow, FAST, "succeeded", "float32")[0] == "green"
    assert (
        judge_performance(FAST, {**FAST, "time_per_step": 1.1e-3}, "succeeded", "float32")[0]
        == "yellow"
    )
    assert judge_performance(FAST, slow, "succeeded", "float32")[0] == "red"  # slower
    wrong = {**FAST, "agrees_with_numpy": False}
    evidence, checks = judge_performance(slow, wrong, "succeeded", "float32")
    assert evidence == "red" and next(c for c in checks if c.name == "same_result").passed is False


def test_only_approved_kernel_code_counts() -> None:
    variant = VariantEvaluation(label="k", role="candidate", job_id="j", job_state="succeeded",
                                metrics=FAST)  # fmt: skip
    approved = {"kernel_hashes": {"benchmarks/advection/kernels/advect.cu": "a" * 64}}
    ran = {"environment": {"kernels": {"advect.cu": "a" * 64}}}
    assert integrity_problem(variant, approved, ran) is None
    tampered = {"environment": {"kernels": {"advect.cu": "b" * 64}}}
    assert "differs from the code that was approved" in (
        integrity_problem(variant, approved, tampered) or ""
    )
    assert "did not report" in (integrity_problem(variant, approved, {}) or "")


def test_shared_gpu_makes_speed_inconclusive_not_red() -> None:
    spec = ExperimentSpec.model_validate({**perf_spec(), "host_id": "h", "backend": "cuda"})

    def job(label: str, metrics: dict[str, Any], caveat: str | None) -> dict[str, Any]:
        env = {"device": "NVIDIA GB10", **({"timing_caveat": caveat} if caveat else {})}
        return {"id": label, "label": label, "state": "succeeded", "manifest": "{}",
                "metrics": dumps(metrics),
                "results": dumps({"backend": "cuda", "environment": env})}  # fmt: skip

    exp = {"id": "e", "title": "t", "spec": dumps(spec.model_dump())}
    slow = {**FAST, "time_per_step": 5e-3}
    shared = "the GPU was shared (6 other processes, 96% busy before the run)"
    report = evaluate(exp, [job("array", FAST, None), job("kernel", slow, shared)], {})
    verdict = report.verdicts[0]
    speed = next(c for c in verdict.checks if c.name == "speed")
    assert verdict.evidence == "yellow" and speed.passed is None and "shared" in speed.detail


# -- end to end on this Mac's GPU ----------------------------------------------------


@pytest.mark.skipif(not HAS_MLX, reason="needs MLX (Apple Silicon)")
def test_metal_kernel_vs_array_end_to_end(client: TestClient) -> None:
    exp = client.post("/experiments", json=perf_spec(host_id="local", backend="metal"))
    assert exp.status_code == 201, exp.text
    approval = client.get("/approvals?status=pending").json()[0]
    assert approval["details"]["objective"] == "performance"
    assert approval["details"]["estimated_seconds"] > 0
    client.post(f"/approvals/{approval['id']}/approve")
    done = wait_for(
        lambda: client.get(f"/experiments/{exp.json()['id']}").json(),
        lambda e: e["state"] in ("reported", "failed"),
        timeout=300,
    )
    assert done["state"] == "reported", done
    verdict = done["evaluation"]["verdicts"][0]
    assert verdict["evidence"] in ("green", "yellow"), verdict  # timing may vary; never red
    same = next(c for c in verdict["checks"] if c["name"] == "same_result")
    assert same["passed"] is True
    report = client.get(f"/experiments/{exp.json()['id']}/report").text
    assert "## Speed and roofline" in report and "Kernel `advect.metal`" in report
    from newton_agentd.runners.bundle import kernel_hashes

    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    for row in ctx.db.query(
        "SELECT label, manifest FROM jobs WHERE experiment_id = ?", (exp.json()["id"],)
    ):
        manifest = json.loads(row["manifest"])
        assert manifest["exclusive"] is True
        if row["label"] == "kernel":  # what was approved is pinned in the job
            assert manifest["kernel_hashes"] == kernel_hashes(ctx.settings.benchmarks_dir)


def test_speed_jobs_run_alone(client: TestClient) -> None:
    from test_orchestration_rules import add_job, stub_host

    stub = stub_host(client, GB10)
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    running = add_job(client, stub.host_id, "cpu", "running")
    queued = add_job(client, stub.host_id, "cpu")
    for job_id in (running, queued):
        row = ctx.db.query_one("SELECT manifest FROM jobs WHERE id = ?", (job_id,))
        manifest = {**json.loads(row["manifest"]), "exclusive": True}
        ctx.db.execute("UPDATE jobs SET manifest = ? WHERE id = ?", (json.dumps(manifest), job_id))
    import time

    time.sleep(1.0)
    assert client.get(f"/jobs/{queued}").json()["state"] == "queued"  # waits its turn


def test_wrong_numbers_are_red_in_accuracy_experiments_too() -> None:
    spec = ExperimentSpec.model_validate({
        "title": "t", "host_id": "h", "backend": "cuda",
        "variants": [{"role": "baseline", "label": "b", "params": {"scheme": "upwind"}},
                     {"role": "candidate", "label": "c", "params": {"scheme": "lax_wendroff"}}],
    })  # fmt: skip
    good = {"stable": True, "conservation": 0.0, "l2_error": 1e-3, "observed_order": 1.0,
            "max_overshoot": 0.0, "runtime": 1.0}  # fmt: skip

    def job(label: str, metrics: dict[str, Any], manifest: dict[str, Any], env: dict[str, Any]
            ) -> dict[str, Any]:  # fmt: skip
        return {"id": label, "label": label, "state": "succeeded", "manifest": dumps(manifest),
                "metrics": dumps(metrics),
                "results": dumps({"backend": "cuda",
                                  "environment": {"device": "NVIDIA GB10", **env}})}  # fmt: skip

    exp = {"id": "e", "title": "t", "spec": dumps(spec.model_dump())}
    better = {**good, "l2_error": 1e-5, "observed_order": 2.0}
    wrong = evaluate(
        exp, [job("b", good, {}, {}), job("c", {**better, "agrees_with_numpy": False}, {}, {})], {}
    )
    assert wrong.verdicts[0].evidence == "red" and wrong.verdicts[0].checks[0].name == "integrity"
    approved = {"kernel_hashes": {"benchmarks/advection/kernels/advect.cu": "a" * 64}}
    tampered = evaluate(exp, [job("b", good, {}, {}),
                              job("c", better, approved, {"kernels": {"advect.cu": "b" * 64}})],
                        {})  # fmt: skip
    assert tampered.verdicts[0].evidence == "red"
    unstable = evaluate(exp, [job("b", good, {}, {}),
                              job("c", {**better, "stable": False}, {},
                                  {"timing_caveat": "Low Power Mode was on"})], {})  # fmt: skip
    assert unstable.verdicts[0].evidence == "red"  # a caveat never hides a real failure
    grids = evaluate(exp, [job("b", {**good, "l2_grid": 1024}, {}, {}),
                           job("c", {**better, "l2_grid": 256}, {}, {})], {})  # fmt: skip
    accuracy = next(c for c in grids.verdicts[0].checks if c.name == "accuracy")
    assert accuracy.passed is None and "finest usable grids differ" in accuracy.detail


def test_placement_says_why() -> None:
    mac = {**MAC, "packages": {"mlx": "0.32.3"}}
    hosts = [host("local", "local", "online", mac)]
    with pytest.raises(PlacementError, match="Apple GPUs have no float64"):
        place(hosts, "auto", "auto", True, 4096, needs_gpu=True)
