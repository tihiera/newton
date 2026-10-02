"""E2 through agentd: schemes as documents, pinned at approval, run as generated code,
their claims checked, the result tied to exactly the document that was approved."""

from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, make_settings, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.contracts import VariantEvaluation
from newton_agentd.research import schemes
from newton_agentd.research.evaluation import integrity_problem

ROOT = Path(__file__).resolve().parents[3]
IR = schemes.ir_module(ROOT / "benchmarks")


def run(client: TestClient, candidate: dict[str, Any], **params: Any) -> dict[str, Any]:
    common = {"resolutions": [64, 128, 256], "repeats": 1, **params}
    r = client.post("/experiments", json={
        "title": "ir candidate", "host_id": "local", "backend": "cpu",
        "variants": [
            {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **common}},
            {"role": "candidate", "label": candidate["name"],
             "params": {"scheme": "ir", "scheme_ir": candidate, **common}},
        ],
    })  # fmt: skip
    assert r.status_code == 201, r.text
    approval = client.get("/approvals?status=pending").json()[0]
    pinned = [v for v in approval["details"]["variants"] if v["role"] == "candidate"][0]
    assert pinned["scheme_ir_digest"] == IR.digest(candidate)  # what is approved, by hash
    client.post(f"/approvals/{approval['id']}/approve")
    done: dict[str, Any] = wait_for(lambda: client.get(f"/experiments/{r.json()['id']}").json(),
                                    lambda e: e["state"] in ("reported", "failed"),
                                    timeout=180)  # fmt: skip
    assert done["state"] == "reported", done
    return done


def test_the_library_and_the_rules(client: TestClient) -> None:
    library = {s["name"]: s for s in client.get("/schemes").json()}
    assert {"upwind", "lax_wendroff", "muscl_koren", "muscl_vanleer_ssprk2"} <= set(library)
    assert library["lax_wendroff"]["hand_written"] is True
    assert library["muscl_koren"]["hand_written"] is False
    ok = client.post("/schemes/check", json=IR.LIBRARY["muscl_mc"]).json()
    assert ok["digest"] == library["muscl_mc"]["digest"] and ok["header"] == "ir:muscl_mc.h"
    bad = {**IR.LIBRARY["muscl_mc"], "flux": {"limiter": "exec", "correction": 0.5}}
    assert client.post("/schemes/check", json=bad).status_code == 400
    r = client.post("/experiments", json={
        "title": "x", "host_id": "local", "backend": "cpu", "variants": [
            {"role": "baseline", "label": "a", "params": {"scheme": "upwind"}},
            {"role": "candidate", "label": "b", "params": {"scheme": "ir"}}]})  # fmt: skip
    assert r.status_code == 422  # 'ir' without a document


def test_a_new_scheme_from_a_document_runs_and_its_claims_hold(client: TestClient) -> None:
    done = run(client, IR.LIBRARY["muscl_koren"])
    verdict = done["evaluation"]["verdicts"][0]
    claims = next(c for c in verdict["checks"] if c["name"] == "claims")
    assert claims["passed"] is True
    candidate = next(v for v in done["evaluation"]["variants"] if v["role"] == "candidate")
    assert {a["claim"] for a in candidate["assumptions"]} >= {"order", "max_cfl", "tvd"}
    assert verdict["evidence"] in ("green", "yellow")
    report = client.get(f"/experiments/{done['id']}/report").text
    assert "## Assumption checks" in report and "muscl_koren | max_cfl" in report


def test_a_scheme_that_overclaims_is_not_green(client: TestClient) -> None:
    bold = copy.deepcopy(IR.LIBRARY["lax_wendroff"])
    bold["name"] = "lw_claims_too_much"
    bold["claims"] = {"order": 2, "max_cfl": 1.5, "tvd": False}
    done = run(client, bold)
    verdict = done["evaluation"]["verdicts"][0]
    claims = next(c for c in verdict["checks"] if c["name"] == "claims")
    assert claims["passed"] is False and "max_cfl" in claims["detail"]
    assert verdict["evidence"] != "green"  # better than upwind, but not as claimed


def test_results_from_another_document_are_red() -> None:
    v = VariantEvaluation(label="c", role="candidate", job_id="j", job_state="succeeded")
    approved = {"scheme_ir_digest": "a" * 64}
    assert "differs from the one that was approved" in (
        integrity_problem(v, approved, {"environment": {"scheme_ir": "b" * 64}}) or ""
    )
    assert integrity_problem(v, approved, {"environment": {"scheme_ir": "a" * 64}}) is None
    flaky = VariantEvaluation(label="c", role="candidate", job_id="j", job_state="succeeded",
                              assumptions=[{"claim": "deterministic", "holds": False}])  # fmt: skip
    assert "not deterministic" in (integrity_problem(flaky, {}, {}) or "")


HOST = os.environ.get("NEWTON_TEST_SSH_HOST")


@pytest.mark.live
@pytest.mark.skipif(not HOST, reason="NEWTON_TEST_SSH_HOST not set")
def test_live_ir_lax_wendroff_is_the_hand_written_cuda_kernel_bit_for_bit(tmp_path: Path) -> None:
    """E2 acceptance: on the GB10, the IR form of Lax–Wendroff, compiled from generated
    code, gives the hand-written kernel's float64 results bit for bit."""
    settings = make_settings(tmp_path, known_hosts_path=tmp_path / "known_hosts")
    with TestClient(create_app(settings), headers=AUTH) as client:
        host = client.post("/hosts", json={"name": "live", "ssh_target": HOST}).json()
        assert client.post(f"/hosts/{host['id']}/bootstrap", timeout=900).status_code == 200
        wait_for(lambda: client.get(f"/hosts/{host['id']}").json(),
                 lambda h: (h.get("gpu_task") or {}).get("state") != "running",
                 timeout=3600, interval=5)  # fmt: skip
        common = {"implementation": "kernel", "precision": "float64", "repeats": 1,
                  "resolutions": [128, 256, 512, 1024], "initial_condition": "square"}  # fmt: skip
        t0 = time.time()
        r = client.post("/experiments", json={
            "title": "IR Lax-Wendroff vs hand-written (CUDA, fp64)", "host_id": host["id"],
            "backend": "cuda", "benchmark": "linear_advection_2d",
            "variants": [
                {"role": "baseline", "label": "hand",
                 "params": {"scheme": "lax_wendroff", **common}},
                {"role": "candidate", "label": "ir",
                 "params": {"scheme": "ir", "scheme_ir": IR.LIBRARY["lax_wendroff"], **common}},
            ],
        })  # fmt: skip
        assert r.status_code == 201, r.text
        approval = client.get("/approvals?status=pending").json()[0]
        client.post(f"/approvals/{approval['id']}/approve")
        done = wait_for(lambda: client.get(f"/experiments/{r.json()['id']}").json(),
                        lambda e: e["state"] in ("reported", "failed"), timeout=1800,
                        interval=2)  # fmt: skip
        assert done["state"] == "reported", done
        jobs = {j["label"]: j for j in client.get(f"/jobs?experiment_id={r.json()['id']}").json()}
        hand = [x["solution_sha256"] for x in jobs["hand"]["results"]["runs"]]
        gen = [x["solution_sha256"] for x in jobs["ir"]["results"]["runs"]]
        print(f"done in {time.time() - t0:.0f} s; hand {hand}; ir {gen}")
        print("kernels:", jobs["ir"]["results"]["environment"]["kernels"])
        assert hand == gen  # bit for bit, every grid
        verdict = done["evaluation"]["verdicts"][0]
        assert not any(c["name"] == "integrity" for c in verdict["checks"]), verdict


@pytest.mark.parametrize(
    ("evidence", "assumptions", "expected", "passed"),
    [
        ("green", [{"claim": "order", "claimed": 2, "measured": 2.0, "holds": True}],
         "green", True),
        ("green", [{"claim": "tvd", "claimed": True, "measured": 0.1, "holds": False}],
         "yellow", False),
        ("green", [{"claim": "order", "claimed": 3, "measured": 2.0, "holds": False}],
         "yellow", False),
        ("red", [{"claim": "max_cfl", "claimed": 1.5, "measured": {}, "holds": False}],
         "red", False),  # never better than it was
        ("yellow", [{"claim": "order", "claimed": 2, "measured": None, "holds": None}],
         "yellow", None),  # nothing tested: no check
        ("green", [{"claim": "deterministic", "holds": False}], "green", None),  # red elsewhere
    ],
)  # fmt: skip
def test_broken_claims_cap_the_verdict(evidence: str, assumptions: list[dict[str, Any]],
                                       expected: str, passed: bool | None) -> None:  # fmt: skip
    from newton_agentd.research.evaluation import check_claims

    v = VariantEvaluation(label="c", role="candidate", job_id="j", job_state="succeeded",
                          assumptions=assumptions)  # fmt: skip
    got, checks = check_claims(evidence, [], v)  # type: ignore[arg-type]
    assert got == expected
    claim_checks = [c for c in checks if c.name == "claims"]
    assert (claim_checks[0].passed if claim_checks else None) is passed


def test_integrity_cases() -> None:
    ok = VariantEvaluation(label="c", role="candidate", job_id="j", job_state="succeeded")
    approved = {"scheme_ir_digest": "a" * 64,
                "kernel_hashes": {"benchmarks/advection/kernels/advect.cu": "1" * 64,
                                  "ir:x.h": "2" * 64}}  # fmt: skip
    good = {"environment": {"scheme_ir": "a" * 64,
                            "kernels": {"advect.cu": "1" * 64, "ir:x.h": "2" * 64}}}  # fmt: skip
    assert integrity_problem(ok, approved, good) is None
    assert integrity_problem(ok, approved, {"environment": {}})  # no digest reported
    other_header = {
        "environment": {
            "scheme_ir": "a" * 64,
            "kernels": {"advect.cu": "1" * 64, "ir:y.h": "2" * 64},
        }
    }
    assert "differs from the code" in (integrity_problem(ok, approved, other_header) or "")
    failed = VariantEvaluation(label="c", role="candidate", job_id="j", job_state="failed")
    assert integrity_problem(failed, {"scheme_ir_digest": "a" * 64}, {"environment": {}}) is None
