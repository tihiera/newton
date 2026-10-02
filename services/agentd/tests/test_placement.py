"""Step 3 (G3): what each host can run, where "auto" puts an experiment, and the
guarantees around GPU jobs (refuse early, one at a time, verify where they ran)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd.orchestration.capabilities import PlacementError, capabilities, place
from newton_agentd.orchestration.jobs import create_job, selftest_manifest
from newton_agentd.research.evaluation import evaluate
from newton_agentd.runners.bundle import build_bundle
from newton_agentd.storage.db import dumps
from test_scheduler_resilience import StubRunner

GB10 = {
    "arch": "aarch64",
    "gpus": [
        {
            "name": "NVIDIA GB10",
            "compute_cap": "12.1",
            "unified_memory": True,
            "driver_version": "580.95.05",
        }
    ],  # fmt: skip
    "packages": {"numpy": "2.1.0", "cupy": "14.2.0"},
    # The worker's smoke test passed for exactly this CuPy and driver.
    "gpu_support": {"ok": True, "cupy": "14.2.0", "driver": "580.95.05", "dist": "cupy-cuda13x"},
}
MAC = {
    "arch": "arm64",
    "gpus": [],
    "apple_gpu": {"name": "Apple M3 Max", "cores": 30},
    "packages": {"numpy": "2.1.0", "mlx": None},
}


def host(id_: str, kind: str, status: str, hardware: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": id_,
        "name": id_,
        "kind": kind,
        "status": status,
        "capabilities": capabilities(hardware),
    }


def test_capabilities_explain_themselves() -> None:
    caps = capabilities(GB10)
    assert caps["cuda"]["ok"] and caps["cuda"]["device"] == "NVIDIA GB10"
    no_cupy = capabilities({**GB10, "packages": {"cupy": None}, "gpu_support": None})
    assert not no_cupy["cuda"]["ok"] and "install GPU support" in no_cupy["cuda"]["reason"]


def test_cuda_counts_only_once_verified_for_this_cupy_and_driver() -> None:
    def cuda(**support: Any) -> dict[str, Any]:
        merged = {**GB10["gpu_support"], **support} if support else None  # type: ignore[dict-item]
        return capabilities({**GB10, "gpu_support": merged})["cuda"]

    assert cuda()["ok"] is False and "not verified" in cuda()["reason"]  # never smoke-tested
    assert cuda(ok=True)["ok"]
    assert not cuda(cupy="14.0.1")["ok"]  # verified an older CuPy: re-verify
    assert "not verified with driver 580.95.05" in cuda(driver="575.51.03")["reason"]
    assert not cuda(driver=None)["ok"]  # recorded while nvidia-smi failed: not for this driver
    failed = cuda(ok=False, error="the CUDA smoke test crashed (SIGBUS)")
    assert not failed["ok"] and "SIGBUS" in failed["reason"]
    install_failed = capabilities(
        {**GB10, "packages": {"cupy": None},
         "gpu_support": {"ok": False, "cupy": None, "error": "installing cupy failed"}}
    )["cuda"]  # fmt: skip
    assert install_failed["reason"] == "installing cupy failed"
    deferred = cuda(ok=False, deferred=True, error="jobs were running")
    assert "waits until the host's running jobs finish" in deferred["reason"]
    broken = capabilities({"gpus": [], "gpu_error": "nvidia-smi failed: Driver/library mismatch"})
    assert "Driver/library mismatch" in broken["cuda"]["reason"]
    mac = capabilities(MAC)
    assert not mac["metal"]["ok"] and "MLX" in mac["metal"]["reason"]
    assert capabilities(None)["cuda"]["reason"] == "host not checked yet"


def test_auto_placement_prefers_the_gb10() -> None:
    mac_with_mlx = {**MAC, "packages": {"mlx": "0.32.3"}}
    hosts = [
        host("local", "local", "online", mac_with_mlx),
        host("spark", "ssh", "online", GB10),
    ]
    h, backend, why = place(hosts, "auto", "auto", uses_float64=True)
    assert (h["id"], backend) == ("spark", "cuda") and "auto" in why
    # Spark offline: big float32 work goes to the Mac GPU, float64 to the Mac CPU.
    hosts[1]["status"] = "error:unreachable"
    big, small = 1 << 17, 1024
    assert place(hosts, "auto", "auto", uses_float64=False, max_cells=big)[1] == "metal"
    assert place(hosts, "auto", "auto", uses_float64=True, max_cells=big)[1] == "cpu"
    # Small runs are faster in numpy than on an Apple GPU: auto keeps them on the CPU,
    # but an explicit metal request is honoured.
    assert place(hosts, "auto", "auto", uses_float64=False, max_cells=small)[1] == "cpu"
    assert place(hosts, "local", "metal", uses_float64=False, max_cells=small)[1] == "metal"


def test_cpu_fallback_stays_on_this_mac() -> None:
    no_cupy = {**GB10, "packages": {"cupy": None}}
    hosts = [host("local", "local", "online", MAC), host("spark", "ssh", "online", no_cupy)]
    h, backend, _ = place(hosts, "auto", "auto", uses_float64=True)
    assert (h["id"], backend) == ("local", "cpu")


def test_fixed_choices_are_checked() -> None:
    hosts = [host("local", "local", "online", MAC)]
    with pytest.raises(PlacementError, match="no NVIDIA GPU"):
        place(hosts, "local", "cuda", uses_float64=True)
    with pytest.raises(PlacementError, match="no connected host can run cuda"):
        place(hosts, "auto", "cuda", uses_float64=True)
    assert place(hosts, "local", "auto", uses_float64=True)[1] == "cpu"


def spec(**overrides: Any) -> dict[str, Any]:
    small = {"resolutions": [32, 64], "repeats": 1}
    return {
        "title": "t",
        "variants": [
            {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **small}},
            {"role": "candidate", "label": "lw", "params": {"scheme": "lax_wendroff", **small}},
        ],
        **overrides,
    }


def set_hardware(client: TestClient, host_id: str, hardware: dict[str, Any], status: str) -> None:
    client.app.state.ctx.db.execute(  # type: ignore[attr-defined]
        "UPDATE hosts SET hardware = ?, status = ?, last_checked_at = ? WHERE id = ?",
        (json.dumps(hardware), status, time.time(), host_id),
    )


@pytest.fixture
def mac_without_gpu_support(client: TestClient) -> None:
    """Placement inputs must not depend on the machine running the tests."""
    set_hardware(client, "local", MAC, "online")


@pytest.mark.usefixtures("mac_without_gpu_support")
def test_api_refuses_what_a_host_cannot_run(client: TestClient) -> None:
    r = client.post("/experiments", json=spec(host_id="local", backend="cuda"))
    assert r.status_code == 400
    assert "can't run cuda" in r.json()["error"]
    metal64 = spec(backend="metal")
    assert client.post("/experiments", json=metal64).status_code == 422  # no float64 on Apple


@pytest.mark.usefixtures("mac_without_gpu_support")
def test_api_places_auto_and_records_why(client: TestClient) -> None:
    exp = client.post("/experiments", json=spec()).json()
    assert exp["host_id"] == "local" and exp["spec"]["backend"] == "cpu"

    spark = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"}).json()
    set_hardware(client, spark["id"], GB10, "online")
    exp = client.post("/experiments", json=spec()).json()
    assert exp["host_id"] == spark["id"] and exp["spec"]["backend"] == "cuda"
    approval = next(
        a for a in client.get("/approvals?status=pending").json() if a["subject_id"] == exp["id"]
    )
    assert approval["details"]["device"] == "NVIDIA GB10"
    assert approval["details"]["placement"].startswith("auto: cuda on spark")
    manifests = [j["manifest"] for j in exp["jobs"]]
    assert {m["backend"] for m in manifests} == {"cuda"}


def test_gpu_jobs_run_one_at_a_time(client: TestClient) -> None:
    spark = client.post("/hosts", json={"name": "spark", "ssh_target": "spark"}).json()
    set_hardware(client, spark["id"], GB10, "online")
    stub = StubRunner(spark["id"])
    stub.block_submit.set()  # submits succeed; jobs then stay "running"
    hosts = client.app.state.ctx.hosts  # type: ignore[attr-defined]
    original = hosts.runner

    async def pick(host_id: str) -> Any:
        return stub if host_id == spark["id"] else await original(host_id)

    hosts.runner = pick
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    ids = [
        create_job(
            ctx.db,
            ctx.settings,
            host_id=spark["id"],
            role="selftest",
            label=f"gpu-{i}",
            manifest={**selftest_manifest(), "backend": "cuda"},
            bundle=build_bundle(None, {}),
        )
        for i in range(2)
    ]
    ctx.scheduler.wake()
    states = wait_for(
        lambda: sorted(client.get(f"/jobs/{i}").json()["state"] for i in ids),
        lambda s: "running" in s,
        timeout=30,
    )
    ticks = ctx.scheduler.ticks
    for _ in range(5):  # give the scheduler more passes to (wrongly) start the second
        ctx.scheduler.wake()
        time.sleep(0.2)
    assert ctx.scheduler.ticks > ticks
    states = sorted(client.get(f"/jobs/{i}").json()["state"] for i in ids)
    assert states == ["queued", "running"]


def test_result_from_the_wrong_backend_is_red() -> None:
    small = {"resolutions": [32, 64], "repeats": 1}
    spec_ = {
        "title": "t",
        "host_id": "spark",
        "backend": "cuda",
        "variants": [
            {"role": "baseline", "label": "b", "params": {"scheme": "upwind", **small}},
            {"role": "candidate", "label": "c", "params": {"scheme": "lax_wendroff", **small}},
        ],
    }
    metrics = {"l2_error": 1e-3, "observed_order": 1.0, "runtime": 1.0, "conservation": 0.0,
               "max_overshoot": 0.0, "stable": True}  # fmt: skip

    def job(label: str, backend: str, device: str) -> dict[str, Any]:
        return {
            "id": f"job-{label}",
            "label": label,
            "state": "succeeded",
            "metrics": dumps(metrics),
            "results": dumps({"backend": backend, "environment": {"device": device}}),
        }

    exp = {"id": "exp-1", "title": "t", "spec": dumps(spec_)}
    report = evaluate(exp, [job("b", "cuda", "NVIDIA GB10"), job("c", "cpu", "cpu (arm)")], {})
    verdict = report.verdicts[0]
    assert verdict.evidence == "red"
    assert "ran on cpu" in verdict.summary
