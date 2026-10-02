"""Engine E2: methods as data. A SchemeIR document and its generated code must be the
hand-written scheme, bit for bit; claims are checked against what a run measures."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from benchmarks.advection import ir, kernels, solver
from benchmarks.advection.schemes import SCHEMES, step

HAS_MLX = importlib.util.find_spec("mlx") is not None
NAMED = ("upwind", "lax_wendroff", "muscl_minmod", "muscl_vanleer")


def study(scheme: Any, **overrides: Any) -> dict[str, Any]:
    params = {
        "scheme": "ir" if isinstance(scheme, dict) else scheme,
        "scheme_ir": scheme if isinstance(scheme, dict) else None,
        "dims": 1, "mode": "convergence", "implementation": "array",
        "resolutions": [64, 128, 256, 512], "cfl": 0.5, "velocity": 1.0, "velocity_y": 0.5,
        "t_final": 1.0, "initial_condition": "sine", "precision": "float64", "repeats": 1,
        "steps": 20, **overrides,
    }  # fmt: skip
    return solver.run_study(params, "cpu")


@pytest.mark.parametrize("name", NAMED)
@pytest.mark.parametrize("precision", ["float64", "float32"])
def test_the_ir_form_is_the_hand_written_scheme_bit_for_bit(name: str, precision: str) -> None:
    doc = ir.validate(ir.LIBRARY[name])
    rng = np.random.default_rng(7)
    for shape, axes in (((300,), (0,)), ((40, 56), (0, 1))):
        u = rng.standard_normal(shape).astype(precision)
        for axis in axes:
            for c in (0.1, 0.45, 0.8, 1.0):
                hand = step(np, u, c, SCHEMES[name], axis)
                generated = step(np, u, c, ir.numpy_flux(doc), axis)
                assert hand.dtype == generated.dtype
                assert np.array_equal(hand, generated), (name, shape, axis, c)


def test_a_whole_study_is_bit_for_bit_the_same() -> None:
    hand = study("lax_wendroff", dims=2, resolutions=[32, 64])
    generated = study(ir.LIBRARY["lax_wendroff"], dims=2, resolutions=[32, 64])
    assert [r["solution_sha256"] for r in hand["runs"]] == [
        r["solution_sha256"] for r in generated["runs"]
    ]


def test_the_generated_header_is_the_hand_written_one_for_lax_wendroff() -> None:
    """Operation for operation: the generated flux is advect.h's SCHEME 1 branch."""
    header = ir.cpp_header(ir.validate(ir.LIBRARY["lax_wendroff"]))
    assert "T dr = up - u0;" in header and "return u0 + k * dr;" in header
    hand = (kernels.KERNEL_DIR / "advect.h").read_text()
    assert "return u0 + k * dr;" in hand and "return u0 - c * (right - left);" in hand
    assert "return u0 - c * (right - left);" in header
    assert ir.header_hash(ir.LIBRARY["lax_wendroff"]) == ir.header_hash(
        ir.validate(ir.LIBRARY["lax_wendroff"])
    )  # deterministic: approvals and results can name it by hash


@pytest.mark.skipif(not HAS_MLX, reason="needs MLX (Apple Silicon)")
@pytest.mark.parametrize("name", NAMED)
def test_generated_metal_kernels_are_the_hand_written_ones_bit_for_bit(name: str) -> None:
    import mlx.core as mx

    rng = np.random.default_rng(3)
    doc = ir.validate(ir.LIBRARY[name])
    hand = kernels.MetalSweeps(mx, name)
    generated = kernels.MetalSweeps(mx, "ir", ir.cpp_header(doc))
    generated.ir = doc
    for shape, axes in (((48, 80), (0, 1)), ((300,), (0,))):
        u = mx.array(rng.standard_normal(shape).astype(np.float32))
        for axis in axes:
            a = np.array(hand(u, None, 0.45, axis))
            b = np.array(generated(u, None, 0.45, axis))
            assert np.array_equal(a, b), (name, shape, axis)


@pytest.mark.skipif(not HAS_MLX, reason="needs MLX (Apple Silicon)")
def test_new_limiters_and_rk_run_as_metal_kernels() -> None:
    for name in ("muscl_superbee", "muscl_mc", "muscl_koren", "muscl_vanleer_ssprk2"):
        result = solver.run_study({
            "scheme": "ir", "scheme_ir": ir.LIBRARY[name], "dims": 1, "mode": "convergence",
            "implementation": "kernel", "resolutions": [128, 256, 512], "cfl": 0.5,
            "velocity": 1.0, "velocity_y": 0.5, "t_final": 1.0, "initial_condition": "sine",
            "precision": "float32", "repeats": 1, "steps": 20,
        }, "metal")  # fmt: skip
        assert result["reference"]["agrees"], name  # generated kernel == numpy generator
        assert f"ir:{name}.h" in result["environment"]["kernels"]


# -- methods as data, not code ------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"flux": {"limiter": "__import__('os')", "correction": ir.LW}},
        {"flux": {"limiter": "minmod", "correction": {"op": "pow", "args": [2, "c"]}}},
        {"flux": {"limiter": "minmod", "correction": {"op": "mul", "args": ["c", "x"]}}},
        {"flux": {"limiter": "minmod", "correction": "0.5 * (1 - c)"}},
        {"flux": {"limiter": "minmod", "correction": float("nan")}},
        {"flux": {"limiter": "minmod", "correction": None}},
        {"time": {"method": "rk", "tableau": {"a": [[0, 1], [1, 0]], "b": [0.5, 0.5]}}},
        {"time": {"method": "rk", "tableau": {"a": [[0, 0], [1, 0]], "b": [0.5, 0.6]}}},
        {"time": {"method": "rk", "tableau": {"a": [[0, 0], [1, 0]], "b": [0.5, 0.5]}}},
        {"claims": {"order": 9, "max_cfl": 1.0, "tvd": False}},
        {"name": "x; rm -rf /"},
        {"code": "__global__ void evil() {}"},
    ],
)
def test_only_data_from_a_fixed_vocabulary(change: dict[str, Any]) -> None:
    doc = {**copy.deepcopy(ir.LIBRARY["muscl_minmod"]), **change}
    with pytest.raises(ir.IRError):
        ir.validate(doc)


def test_coefficients_are_evaluated_as_written() -> None:
    lw = ir.validate(ir.LIBRARY["lax_wendroff"])
    for c in (0.1, 0.3, 0.7, 0.9):
        assert ir.coefficient(lw, c) == 0.5 * (1.0 - c)  # the same rounding, not 0.5 - 0.5c
    assert kernels.coefficients(0.3, lw) == kernels.coefficients(0.3)


# -- claims vs measurements ------------------------------------------------------------------


def checks(result: dict[str, Any]) -> dict[str, Any]:
    return {a["claim"]: a for a in result["assumptions"]}


def test_true_claims_hold() -> None:
    result = checks(study(ir.LIBRARY["muscl_vanleer_ssprk2"], resolutions=[128, 256, 512, 1024],
                          initial_condition="sine"))  # fmt: skip
    assert result["order"]["holds"] and result["order"]["measured"] > 1.65  # limited
    assert result["conservation"]["holds"] and result["max_cfl"]["holds"]
    assert result["deterministic"]["holds"]
    square = checks(study(ir.LIBRARY["muscl_vanleer_ssprk2"], initial_condition="square"))
    assert square["tvd"]["holds"] is True


def test_false_claims_are_caught() -> None:
    bold = copy.deepcopy(ir.LIBRARY["lax_wendroff"])
    bold["name"] = "lw_overclaimed"
    bold["claims"] = {"order": 3, "max_cfl": 1.5, "tvd": True}
    smooth = checks(study(bold, resolutions=[64, 128, 256, 512]))
    assert smooth["order"]["holds"] is False  # second order, not third
    assert smooth["max_cfl"]["holds"] is False  # Lax-Wendroff blows up past c = 1
    assert smooth["max_cfl"]["measured"]["at_claim"]["method"] == "von Neumann (exact)"
    square = checks(study(bold, initial_condition="square", resolutions=[64, 128, 256]))
    assert square["tvd"]["holds"] is False  # it oscillates at the square wave's edges
    assert square["order"]["holds"] is None  # no formal order on discontinuous data


def test_the_review_round_rules() -> None:
    # An honest scheme on a square wave: order not judged, so not "failed".
    honest = checks(study(ir.LIBRARY["muscl_koren"], initial_condition="square",
                          resolutions=[64, 128, 256]))  # fmt: skip
    assert honest["order"]["holds"] is None and honest["tvd"]["holds"] is True
    # SSP-RK2 MUSCL claims TVD up to 0.5 only, and holds it there.
    rk = checks(study(ir.LIBRARY["muscl_vanleer_ssprk2"], initial_condition="square",
                      cfl=0.5, resolutions=[64, 128, 256]))  # fmt: skip
    assert rk["tvd"]["holds"] is True and rk["max_cfl"]["holds"] is True
    # The stability check scales with c: a tiny claimed CFL can't hide an unstable
    # scheme (forward Euler with a central flux is unstable at every c).
    central = {"name": "fe_central", "flux": {"limiter": "none", "correction": 0.5},
               "time": {"method": "rk", "tableau": {"a": [[0]], "b": [1]}},
               "claims": {"order": 2, "max_cfl": 0.025, "tvd": False}}  # fmt: skip
    probe = solver.stability_probe(ir.validate(central), 0.025)
    assert probe["stable"] is False and probe["growth"] > 1
    # Documents: singular A(c), huge numbers, Unicode names: refused, cleanly.
    for bad in (
        {
            **ir.LIBRARY["lax_wendroff"],
            "flux": {
                "limiter": "none",
                "correction": {"op": "div", "args": [0.5, {"op": "sub", "args": [1.0, "c"]}]},
            },
        },
        {**ir.LIBRARY["lax_wendroff"], "flux": {"limiter": "none", "correction": 10**400}},
        {**ir.LIBRARY["lax_wendroff"], "name": "lw²名"},
    ):
        with pytest.raises(ir.IRError):
            ir.validate(bad)
    # 1 and 1.0 are the same document: the same digest wherever it is computed.
    ints = {
        **ir.LIBRARY["lax_wendroff"],
        "flux": {
            "limiter": "none",
            "correction": {"op": "mul", "args": [0.5, {"op": "sub", "args": [1, "c"]}]},
        },
    }
    assert ir.digest(ints) == ir.digest(ir.LIBRARY["lax_wendroff"])


def test_the_cli_runs_a_scheme_from_a_document(tmp_path: Path) -> None:
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"scheme": "ir", "scheme_ir": ir.LIBRARY["muscl_koren"],
                                  "resolutions": [64, 128, 256], "repeats": 1}))  # fmt: skip
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.advection", "--params", str(params), "--out",
         str(tmp_path / "out"), "--backend", "cpu"],
        cwd=root, capture_output=True, text=True, timeout=300,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "MPLBACKEND": "Agg"},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    results = json.loads((tmp_path / "out" / "results.json").read_text())
    assert results["scheme"] == "muscl_koren" and results["formal_order"] == 2
    assert results["environment"]["scheme_ir"] == ir.digest(ir.LIBRARY["muscl_koren"])
    assert {a["claim"] for a in results["assumptions"]} >= {"order", "max_cfl", "tvd"}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"scheme": "ir", "scheme_ir": {"name": "x"}}))
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.advection", "--params", str(bad), "--out",
         str(tmp_path / "out2"), "--backend", "cpu"],
        cwd=root, capture_output=True, text=True, timeout=300,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "MPLBACKEND": "Agg"},
    )  # fmt: skip
    assert proc.returncode != 0 and "invalid scheme_ir" in proc.stderr
