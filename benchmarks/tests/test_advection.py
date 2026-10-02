"""Numerical sanity of the advection benchmark (CPU)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from benchmarks.advection.solver import cell_averages, run_study


def study(scheme: str, ic: str = "sine", resolutions: list[int] | None = None) -> dict[str, Any]:
    params = {
        "scheme": scheme,
        "resolutions": resolutions or [64, 128, 256, 512],
        "cfl": 0.5,
        "velocity": 1.0,
        "t_final": 1.0,
        "initial_condition": ic,
        "precision": "float64",
        "repeats": 1,
    }
    return run_study(params, "cpu")


@pytest.mark.parametrize(
    ("scheme", "lo", "hi"),
    [("upwind", 0.9, 1.1), ("lax_wendroff", 1.9, 2.1), ("muscl_vanleer", 1.6, 2.1)],
)
def test_observed_order_on_smooth_data(scheme: str, lo: float, hi: float) -> None:
    order = study(scheme)["metrics"]["observed_order"]
    assert lo < order < hi


@pytest.mark.parametrize("scheme", ["upwind", "lax_wendroff", "muscl_minmod", "muscl_vanleer"])
def test_all_schemes_conserve_mass(scheme: str) -> None:
    m = study(scheme, "gaussian", [100, 200])["metrics"]
    assert m["conservation"] < 1e-12
    assert m["stable"]


def test_limiters_are_tvd_on_discontinuities() -> None:
    lw = study("lax_wendroff", "square", [200])["metrics"]
    minmod = study("muscl_minmod", "square", [200])["metrics"]
    assert lw["max_overshoot"] > 0.05  # Gibbs-like oscillations
    assert minmod["max_overshoot"] < 1e-12
    assert minmod["tv_increase"] < 1e-12


def test_square_cell_averages_are_exact() -> None:
    avg = cell_averages("square", 4)
    assert np.allclose(avg, [0, 1, 1, 0])
    shifted = cell_averages("square", 4, shift=0.125)
    assert np.allclose(shifted, [0, 0.5, 1, 0.5])
