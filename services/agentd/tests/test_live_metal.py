"""Live test on this Mac's GPU (Metal via MLX). Opt-in:

    NEWTON_TEST_LOCAL_METAL=1 scripts/test.sh -m live

Runs a float32 experiment on host "local" with backend metal, end to end, and
checks the result really came from the Apple GPU.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import AUTH, make_settings, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("NEWTON_TEST_LOCAL_METAL") != "1", reason="NEWTON_TEST_LOCAL_METAL not set"
    ),
]


def test_metal_end_to_end(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path)), headers=AUTH) as client:
        local = client.post("/hosts/local/check").json()
        assert local["capabilities"]["metal"]["ok"], local["capabilities"]["metal"]
        # Grids float32 can resolve: finer ones are round-off limited (see observed_order).
        res = {"resolutions": [256, 512, 1024], "repeats": 2, "precision": "float32"}
        exp = client.post(
            "/experiments",
            json={
                "title": "live metal run",
                "host_id": "local",
                "backend": "metal",
                "variants": [
                    {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **res}},
                    {
                        "role": "candidate",
                        "label": "vanleer",
                        "params": {"scheme": "muscl_vanleer", **res},
                    },
                ],  # fmt: skip
            },
        )
        assert exp.status_code == 201, exp.text
        approval = client.get("/approvals?status=pending").json()[0]
        client.post(f"/approvals/{approval['id']}/approve")
        done = wait_for(
            lambda: client.get(f"/experiments/{exp.json()['id']}").json(),
            lambda e: e["state"] in ("reported", "failed"),
            timeout=600,
            interval=1,
        )
        assert done["state"] == "reported", done
        report = client.get(f"/experiments/{exp.json()['id']}/report").text
        print(report)
        assert "metal: Apple" in report
        evaluation = done["evaluation"]
        assert evaluation["evidence"] != "unknown", evaluation["summary"]
        assert all(v["backend"] == "metal" for v in evaluation["variants"])


def test_metal_kernels_end_to_end(tmp_path: Path) -> None:
    from test_live_ssh import run_performance

    with TestClient(create_app(make_settings(tmp_path)), headers=AUTH) as client:
        assert client.post("/hosts/local/check").json()["capabilities"]["metal"]["ok"]
        report = run_performance(client, "local", "metal", "float32", side=4096)
        assert "Kernel `advect.metal`" in report
