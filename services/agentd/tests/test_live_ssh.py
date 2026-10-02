"""Live test against a real SSH host. Opt-in:

    NEWTON_TEST_SSH_HOST=<ssh alias> scripts/test.sh -m live

The host key must already be in your known_hosts (or trusted via the API).
Installs the worker into ~/.newton on the host (venv + numpy/matplotlib).
On a host with an NVIDIA GPU, GPU support must end up verified (installed
automatically, or via the Install GPU support action) and the run uses cuda.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import AUTH, make_settings, wait_for, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app

HOST = os.environ.get("NEWTON_TEST_SSH_HOST")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not HOST, reason="NEWTON_TEST_SSH_HOST not set"),
]


def test_live_host_end_to_end(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        host = client.post("/hosts", json={"name": "live", "ssh_target": HOST}).json()
        online = client.post(f"/hosts/{host['id']}/bootstrap", timeout=900)
        assert online.status_code == 200, online.text
        hw = online.json()["hardware"]
        print("hardware:", hw.get("gpus"), hw.get("packages"))

        job = client.post(f"/hosts/{host['id']}/selftest", json={}).json()
        assert wait_job(client, job["id"], {"succeeded", "failed"}, 300)["state"] == "succeeded"

        # GPU support sets itself up in the background once the worker serves.
        settled = wait_for(
            lambda: client.get(f"/hosts/{host['id']}").json(),
            lambda h: (h.get("gpu_task") or {}).get("state") != "running",
            timeout=3600,
            interval=5,
        )
        cuda = settled["capabilities"]["cuda"]
        if hw.get("gpus") and not cuda["ok"]:
            # "auto" may have deferred or failed: the button retries and says why.
            r = client.post(f"/hosts/{host['id']}/gpu-support?wait=true", timeout=3600)
            print("gpu support:", r.status_code, r.text[-1500:])
            assert r.status_code == 200, r.text
            cuda = r.json()["capabilities"]["cuda"]
        if hw.get("gpus"):
            support = client.get(f"/hosts/{host['id']}").json()["hardware"].get("gpu_support")
            print("cuda:", cuda, "\ngpu_support:", support)
            assert cuda["ok"], cuda
        backend = "cuda" if cuda["ok"] else "cpu"
        res = {"resolutions": [256, 512, 1024, 2048], "repeats": 2}
        exp = client.post(
            "/experiments",
            json={
                "title": f"live {backend} run",
                "host_id": host["id"],
                "backend": backend,
                "variants": [
                    {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **res}},
                    {
                        "role": "candidate",
                        "label": "vanleer",
                        "params": {"scheme": "muscl_vanleer", **res},
                    },
                ],  # fmt: skip
            },
        ).json()
        approval = client.get("/approvals?status=pending").json()[0]
        client.post(f"/approvals/{approval['id']}/approve")
        done = wait_for(
            lambda: client.get(f"/experiments/{exp['id']}").json(),
            lambda e: e["state"] in ("reported", "failed"),
            timeout=900,
            interval=1,
        )
        assert done["state"] == "reported"
        print(client.get(f"/experiments/{exp['id']}/report").text)


def run_performance(
    client: TestClient, host_id: str, backend: str, precision: str, side: int = 2048
) -> str:
    """P1 acceptance: hand-written kernel vs array code on one large 2D grid (big
    enough not to fit in the device's cache, so the roofline fraction is given)."""
    common = {"mode": "throughput", "resolutions": [side], "steps": 50,
              "precision": precision, "repeats": 2}  # fmt: skip
    exp = client.post(
        "/experiments",
        json={
            "title": f"live {backend} kernel vs array (2D, {precision})",
            "benchmark": "linear_advection_2d",
            "objective": "performance",
            "host_id": host_id,
            "backend": backend,
            "variants": [
                {"role": "baseline", "label": "array",
                 "params": {"scheme": "muscl_vanleer", "implementation": "array", **common}},
                {"role": "candidate", "label": "kernel",
                 "params": {"scheme": "muscl_vanleer", "implementation": "kernel", **common}},
            ],
        },
    )  # fmt: skip
    assert exp.status_code == 201, exp.text
    approval = client.get("/approvals?status=pending").json()[0]
    print(
        "estimate:", approval["details"]["estimated_seconds"], approval["details"]["estimate_basis"]
    )
    client.post(f"/approvals/{approval['id']}/approve")
    done = wait_for(
        lambda: client.get(f"/experiments/{exp.json()['id']}").json(),
        lambda e: e["state"] in ("reported", "failed"),
        timeout=1800,
        interval=2,
    )
    assert done["state"] == "reported", done
    report: str = client.get(f"/experiments/{exp.json()['id']}/report").text
    print(report)
    verdict = done["evaluation"]["verdicts"][0]
    same = next(c for c in verdict["checks"] if c["name"] == "same_result")
    assert same["passed"] is True, same  # the kernel computes numpy's numbers
    assert verdict["evidence"] != "red", verdict
    assert "## Speed and roofline" in report
    kernel = next(v for v in done["evaluation"]["variants"] if v["label"] == "kernel")
    assert kernel["metrics"]["bandwidth_fraction"], kernel["metrics"]  # the % of peak
    return report


def test_live_gpu_scale_on_cuda(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        host = client.post("/hosts", json={"name": "live", "ssh_target": HOST}).json()
        assert client.post(f"/hosts/{host['id']}/bootstrap", timeout=900).status_code == 200
        settled = wait_for(
            lambda: client.get(f"/hosts/{host['id']}").json(),
            lambda h: (h.get("gpu_task") or {}).get("state") != "running",
            timeout=3600,
            interval=5,
        )
        if not settled["capabilities"]["cuda"]["ok"]:
            pytest.skip(f"no usable CUDA on {HOST}: {settled['capabilities']['cuda']}")
        report = run_performance(client, host["id"], "cuda", "float64")
        assert "Kernel `advect.cu`" in report
