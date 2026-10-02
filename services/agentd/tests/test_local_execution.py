"""B1/B2/B3 on the local runner: jobs, approvals, experiments, reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import wait_for, wait_job
from fastapi.testclient import TestClient

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "rejected"}


def test_local_host_check_reports_hardware(client: TestClient) -> None:
    host = client.post("/hosts/local/check").json()
    assert host["status"] == "online"
    assert host["hardware"]["cpu_count"] >= 1
    assert host["hardware"]["worker"]["ok"] is True


def test_selftest_job_roundtrip(client: TestClient) -> None:
    job = client.post("/hosts/local/selftest", json={}).json()
    done = wait_job(client, job["id"], TERMINAL)
    assert done["state"] == "succeeded", done
    assert done["attempt"] == 1
    assert done["metrics"]["checksum"] > 0

    logs = client.get(f"/jobs/{job['id']}/logs").json()
    assert "selftest done" in logs["data"]
    files = client.get(f"/jobs/{job['id']}/artifacts").json()
    assert "artifacts/hello.txt" in files
    hello = client.get(f"/jobs/{job['id']}/artifacts/artifacts/hello.txt")
    assert hello.text.startswith("hello from the newton worker")
    assert client.get(f"/jobs/{job['id']}/artifacts/..%2F..%2Fapi-token").status_code in (
        400,
        404,
    )

    states = [
        e["data"]["to"]
        for e in client.get(f"/events?entity_id={job['id']}").json()
        if e["kind"] == "state"
    ]
    assert states == ["submitting", "running", "collecting", "succeeded"]


def test_failed_job_is_a_result_not_a_retry(client: TestClient) -> None:
    job = client.post("/hosts/local/selftest", json={"fail": True}).json()
    done = wait_job(client, job["id"], TERMINAL)
    assert done["state"] == "failed"
    assert done["attempt"] == 1
    assert "selftest asked to fail" in done["error"]  # stderr tail, not just "exit code 1"


def test_cancel_running_job(client: TestClient) -> None:
    job = client.post("/hosts/local/selftest", json={"sleep": 30}).json()
    wait_job(client, job["id"], {"running"})
    client.post(f"/jobs/{job['id']}/cancel")
    assert wait_job(client, job["id"], TERMINAL)["state"] == "cancelled"


def spec(**overrides: Any) -> dict[str, Any]:
    small = {"resolutions": [32, 64, 128], "repeats": 1}
    return {
        "title": "Higher-order advection vs upwind",
        "host_id": "local",
        "hypothesis": "Second-order schemes cut error without losing conservation.",
        "variants": [
            {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **small}},
            {
                "role": "candidate",
                "label": "lax-wendroff",
                "params": {"scheme": "lax_wendroff", **small},
            },
            {
                "role": "candidate",
                "label": "muscl-minmod",
                "params": {"scheme": "muscl_minmod", **small},
            },
        ],  # fmt: skip
        **overrides,
    }


def test_experiment_requires_approval_then_reports(client: TestClient) -> None:
    goal = client.post("/goals", json={"title": "Beat upwind"}).json()
    exp = client.post("/experiments", json=spec(goal_id=goal["id"])).json()
    assert exp["state"] == "awaiting_approval"
    assert {j["state"] for j in exp["jobs"]} == {"pending_approval"}

    # Nothing runs before approval.
    pending = client.get("/approvals?status=pending").json()
    assert [a["subject_id"] for a in pending] == [exp["id"]]
    assert pending[0]["details"]["variants"][0]["label"] == "upwind"

    client.post(f"/approvals/{pending[0]['id']}/approve", json={"note": "go"})
    assert client.post(f"/approvals/{pending[0]['id']}/approve").status_code == 409

    done = wait_for(
        lambda: client.get(f"/experiments/{exp['id']}").json(),
        lambda e: e["state"] in ("reported", "failed"),
        timeout=120,
    )
    assert done["state"] == "reported", done.get("error")
    assert all(j["state"] == "succeeded" for j in done["jobs"])

    evaluation = done["evaluation"]
    verdicts = {v["label"]: v for v in evaluation["verdicts"]}
    # Lax–Wendroff is second order on a smooth sine: far lower error than upwind.
    lw = verdicts["lax-wendroff"]
    assert lw["evidence"] in ("green", "yellow"), lw
    checks = {c["name"]: c for c in lw["checks"]}
    assert checks["accuracy"]["passed"] is True
    assert checks["conservation"]["passed"] is True
    assert checks["convergence_order"]["passed"] is True
    assert evaluation["evidence"] in ("green", "yellow")

    base = next(v for v in evaluation["variants"] if v["role"] == "baseline")
    assert 0.8 < base["metrics"]["observed_order"] < 1.2

    report = client.get(f"/experiments/{exp['id']}/report").text
    assert "## Verdicts" in report and "## Reproduce" in report and "## Provenance" in report
    assert Path(done["report_path"]).is_file()


def test_rejected_experiment_never_runs(client: TestClient) -> None:
    exp = client.post("/experiments", json=spec()).json()
    approval = client.get("/approvals?status=pending").json()[0]
    client.post(f"/approvals/{approval['id']}/reject", json={"note": "not now"})
    after = client.get(f"/experiments/{exp['id']}").json()
    assert after["state"] == "rejected"
    assert {j["state"] for j in after["jobs"]} == {"rejected"}
    assert all(j["attempt"] == 0 for j in after["jobs"])


def test_experiment_spec_validation(client: TestClient) -> None:
    no_baseline = spec()
    no_baseline["variants"][0]["role"] = "candidate"
    assert client.post("/experiments", json=no_baseline).status_code == 422
    bad_param = spec()
    bad_param["variants"][1]["params"]["scheme"] = "rm -rf /"
    assert client.post("/experiments", json=bad_param).status_code == 422
    assert client.post("/experiments", json=spec(host_id="nope")).status_code == 404
