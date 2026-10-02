"""P1: 2D dimension-split advection, throughput mode, the numpy reference and the
hand-written kernels (Metal here; CUDA runs in the live GB10 test)."""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from benchmarks.advection import bandwidth, kernels, solver
from benchmarks.advection.backends import ArrayBackend
from benchmarks.advection.schemes import SCHEMES, step

HAS_MLX = importlib.util.find_spec("mlx") is not None


def study(backend: str = "cpu", **overrides: Any) -> dict[str, Any]:
    params = {
        "scheme": "lax_wendroff", "dims": 2, "mode": "convergence", "implementation": "array",
        "resolutions": [32, 64, 128], "cfl": 0.5, "velocity": 1.0, "velocity_y": 0.5,
        "t_final": 0.25, "initial_condition": "sine", "precision": "float64", "repeats": 1,
        "steps": 20, **overrides,
    }  # fmt: skip
    return solver.run_study(params, backend)


@pytest.mark.parametrize(("scheme", "order"), [("upwind", 1.0), ("lax_wendroff", 2.0)])
def test_2d_splitting_keeps_the_formal_order(scheme: str, order: float) -> None:
    result = study(scheme=scheme)
    assert result["metrics"]["observed_order"] == pytest.approx(order, abs=0.15)
    assert result["metrics"]["conservation"] < 1e-12
    assert result["runs"][0]["dims"] == 2 and result["runs"][0]["cells"] == 32 * 32


def test_2d_limiter_stays_tvd() -> None:
    result = study(scheme="muscl_vanleer", initial_condition="square")
    assert result["metrics"]["max_overshoot"] < 1e-12
    assert result["metrics"]["tv_increase"] < 1e-9


def test_alternating_sweeps_beat_fixed_order() -> None:
    # On separable data the x and y sweeps commute, so the order can't matter there.
    # On sin(2 pi (x + y)) a limited scheme's sweeps don't commute: always sweeping x
    # first leaves a first-order splitting error that alternation (xy, yx) cancels.
    params = {"t_final": 0.25, "cfl": 0.8, "velocity": 1.0, "velocity_y": 0.5}
    n = 64
    x = (np.arange(n) + 0.5) / n
    exact_shift = np.sin(2 * np.pi * ((x[None, :] - 0.25) + (x[:, None] - 0.125)))
    be = ArrayBackend()
    stepper = solver.Stepper(be, "muscl_minmod", "float64", "array")
    steps = solver.step_count(params, n, 2)
    courant = solver.courant_numbers(params, n, steps, 2)
    alternating = fixed = np.sin(2 * np.pi * (x[None, :] + x[:, None]))
    for k in range(steps):
        alternating = stepper.advance(alternating, k, courant)
        fixed = stepper.advance(fixed, 0, courant)
    gap = np.abs(fixed - alternating).max()
    assert gap > 1e-6  # the order really matters here
    assert np.abs(alternating - exact_shift).max() < np.abs(fixed - exact_shift).max()


def test_courant_number_is_the_same_on_every_grid() -> None:
    params = {"t_final": 0.3, "cfl": 1.0, "velocity": 1.0, "velocity_y": 0.5}
    courants = [0.3 * n / solver.step_count(params, n, 1, 64) for n in (64, 128, 256, 512)]
    assert max(courants) - min(courants) < 1e-12 and max(courants) <= 1.0
    # A first-order scheme near cfl 1 then still measures as first order.
    result = study(scheme="upwind", dims=1, cfl=1.0, t_final=0.3, resolutions=[64, 128, 256, 512])
    assert result["metrics"]["observed_order"] == pytest.approx(1.0, abs=0.1)
    assert result["observed_order"]["roundoff_limited"] == []


def test_square_wave_exact_solution_after_several_periods() -> None:
    for shift in (0.0, 1.0, 2.5, 3.0, 10.25):
        assert solver.cell_averages("square", 64, shift).mean() == pytest.approx(0.5)
    assert np.allclose(solver.cell_averages("square", 64, 3.25),
                       solver.cell_averages("square", 64, 0.25))  # fmt: skip


def test_throughput_mode_times_one_grid_without_error_metrics() -> None:
    result = study(mode="throughput", resolutions=[128])
    m = result["metrics"]
    assert "l2_error" not in m and m["time_per_step"] > 0 and m["cell_updates_per_s"] > 0
    # numpy compared with itself: no comparison, rather than a fake "1.0x, same".
    assert m["speedup_vs_numpy"] is None and m["agrees_with_numpy"] is None
    assert m["peak_gbps"] > 0
    assert m["cache_resident"] is True and m["bandwidth_fraction"] is None  # 128^2 fits in cache
    cells, steps, seconds = 128 * 128, 20, result["runs"][0]["runtime_s"]
    assert m["achieved_gbps"] == pytest.approx(2 * 8 * cells * 2 * steps / seconds / 1e9)
    assert result["runs"][0]["steps"] == 20 and result["reference"] is None


def run(n: int, err: float, steps: int | None = None) -> dict[str, Any]:
    return {"nx": n, "dx": 1 / n, "steps": steps or 2 * n, "dims": 1, "l2_error": err,
            "stable": True}  # fmt: skip


def test_order_fit_stops_where_rounding_takes_over() -> None:
    # Measured: Lax-Wendroff in float32 (1D sine, t = 1). From 4096 cells on the
    # scheme's correction is rounded away and the error collapses below float64's.
    lw = [run(256, 3.34e-4), run(1024, 1.97e-5), run(4096, 6.01e-8), run(16384, 3.10e-8)]
    fit = solver.observed_order(lw, "float32", 2)
    assert fit["roundoff_limited"] == [4096, 16384] and fit["finest_usable"] == 1024
    assert fit["fit"] == pytest.approx(2.0, abs=0.1)
    assert solver.observed_order(lw, "float64", 2)["roundoff_limited"] == []


def test_order_fit_stops_at_rising_or_stalling_error_near_the_floor() -> None:
    # Measured: Lax-Wendroff float32, t = 0.3: rounding noise grows with the steps.
    rising = [run(1024, 6.27e-6, 615), run(4096, 2.90e-5, 2458), run(8192, 5.97e-5, 4916)]
    fit = solver.observed_order(rising, "float32", 2)
    # Even 1024 is within 3x of float32's accumulated rounding: nothing is trusted,
    # and the order is reported as inconclusive rather than guessed.
    assert fit["roundoff_limited"] == [1024, 4096, 8192] and fit["fit"] is None
    # Stalling well short of the formal order, close to the rounding level.
    stall = [run(512, 1e-3), run(1024, 2.5e-4), run(2048, 2.0e-4)]
    assert solver.observed_order(stall, "float32", 2)["roundoff_limited"] == [2048]
    # The same numbers far above float64's rounding level are kept: pre-asymptotic
    # grids, not round-off, and the fit reports them as measured.
    assert solver.observed_order(stall, "float64", 2)["roundoff_limited"] == []


def test_accuracy_uses_the_finest_usable_grid() -> None:
    result = study(scheme="lax_wendroff", dims=1, precision="float32", t_final=1.0,
                   resolutions=[256, 1024, 4096])  # fmt: skip
    m = result["metrics"]
    assert result["observed_order"]["roundoff_limited"] == [4096]
    assert m["l2_grid"] == 1024 and m["l2_error"] == result["runs"][1]["l2_error"]


def test_cpu_has_no_kernel_implementation() -> None:
    with pytest.raises(ValueError, match="kernel implementation runs on cuda or metal"):
        study(implementation="kernel")


def test_bandwidth_probe_measures_this_machine() -> None:
    roof = bandwidth.probe(ArrayBackend(), "float32")
    assert roof["copy_gbps"] > 0.5 and roof["peak_gbps"] >= roof["copy_gbps"]
    assert bandwidth.achieved(1e9, 8, 1.0) == pytest.approx(16.0)


def test_kernel_sources_are_hashed_and_bundled() -> None:
    hashes = kernels.hashes()
    assert set(hashes) == {"advect.h", "advect.cu", "advect.metal"}
    assert all(len(h) == 64 for h in hashes.values())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "agentd"))
    from newton_agentd.runners.bundle import build_bundle, kernel_hashes

    root = Path(__file__).resolve().parents[1]
    bundled = kernel_hashes(root)
    assert bundled["benchmarks/advection/kernels/advect.cu"] == hashes["advect.cu"]
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(build_bundle(root)), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {f"benchmarks/advection/kernels/{n}" for n in hashes} <= names


@pytest.mark.skipif(not HAS_MLX, reason="needs MLX (Apple Silicon)")
@pytest.mark.parametrize("scheme", sorted(SCHEMES))
def test_metal_kernels_match_numpy_float32(scheme: str) -> None:
    import mlx.core as mx

    rng = np.random.default_rng(1)
    sweep = kernels.MetalSweeps(mx, scheme)
    for shape, axes in (((48, 80), (0, 1)), ((300,), (0,))):
        u = rng.standard_normal(shape).astype(np.float32)
        for axis in axes:
            got = np.array(sweep(mx.array(u), None, 0.45, axis))
            ref = step(np, u, 0.45, SCHEMES[scheme], axis)
            assert np.abs(got - ref).max() <= 4 * np.finfo(np.float32).eps * np.abs(ref).max()


@pytest.mark.skipif(not HAS_MLX, reason="needs MLX (Apple Silicon)")
def test_metal_kernel_study_agrees_and_reports_the_roofline() -> None:
    result = study(
        "metal", scheme="muscl_vanleer", implementation="kernel", mode="throughput",
        resolutions=[512], precision="float32",
    )  # fmt: skip
    m, ref = result["metrics"], result["reference"]
    assert m["agrees_with_numpy"] is True and ref["max_relative_difference"] < 1e-5
    assert m["speedup_vs_numpy"] > 1 and m["peak_gbps"] > 10
    assert result["environment"]["kernels"] == kernels.hashes()
    conv = study("metal", scheme="lax_wendroff", implementation="kernel", precision="float32",
                 resolutions=[64, 128, 256])  # fmt: skip
    assert conv["metrics"]["observed_order"] == pytest.approx(2.0, abs=0.15)


def test_cli_writes_a_2d_throughput_result(tmp_path: Path) -> None:
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"scheme": "upwind", "dims": 2, "mode": "throughput",
                                  "resolutions": [64], "steps": 5, "repeats": 1}))  # fmt: skip
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.advection", "--params", str(params), "--out",
         str(tmp_path / "out"), "--backend", "cpu"],
        cwd=root, capture_output=True, text=True, timeout=300,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "MPLBACKEND": "Agg"},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    results = json.loads((tmp_path / "out" / "results.json").read_text())
    assert results["benchmark"] == "linear_advection_2d"
    assert results["bandwidth"]["peak_gbps"] > 0
    assert math.isfinite(results["metrics"]["time_per_step"])
    assert "ms/step" in proc.stdout


def test_a_backend_that_computes_different_numbers_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    be = ArrayBackend()
    be.name = "cuda"  # pretend: the reference only runs for non-numpy backends
    stepper = solver.Stepper(ArrayBackend(), "lax_wendroff", "float64", "array")
    real = stepper.sweep
    monkeypatch.setattr(stepper, "sweep", lambda u, c, axis: real(u, c * (1 + 1e-6), axis))
    params = {"scheme": "lax_wendroff", "cfl": 0.5, "velocity": 1.0, "velocity_y": 0.5,
              "t_final": 1.0, "initial_condition": "sine", "precision": "float64",
              "repeats": 1, "steps": 5}  # fmt: skip
    ref = solver.numpy_reference(be, stepper, params, 64, 2)
    assert ref["agrees"] is False and ref["max_relative_difference"] > 1e-12
    assert solver.numpy_reference(be, solver.Stepper(be, "lax_wendroff", "float64", "array"),
                                  params, 64, 2)["agrees"] is True  # fmt: skip


def test_a_blown_up_throughput_run_is_unstable(monkeypatch: pytest.MonkeyPatch) -> None:
    real = solver.Stepper.advance
    monkeypatch.setattr(solver.Stepper, "advance",
                        lambda self, u, n, c: real(self, u, n, c) * 1e300)  # fmt: skip
    m = study(mode="throughput", resolutions=[64])["metrics"]
    assert m["stable"] is False and m["conservation"] == math.inf


def test_shared_gpu_caveat() -> None:
    from benchmarks.advection.power import gpu_caveat

    assert gpu_caveat({"utilization_pct": 96, "other_processes": 6}) is not None
    assert gpu_caveat({"utilization_pct": 0, "other_processes": 2}) is None  # idle, holds memory
    assert gpu_caveat(None) is None
    merged = solver.merge_activity(
        {"utilization_pct": 0, "other_processes": 1},
        {"utilization_pct": 80, "other_processes": 3},
        None,
    )
    assert merged == {"utilization_pct": 80, "other_processes": 3}  # fmt: skip
