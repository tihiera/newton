"""The Apple GPU backend on real hardware (skipped elsewhere)."""

from __future__ import annotations

import platform
import sys
from typing import Any

import pytest

if sys.platform != "darwin" or platform.machine() != "arm64":
    pytest.skip("Apple Silicon only", allow_module_level=True)
pytest.importorskip("mlx.core")

from benchmarks.advection import solver  # noqa: E402
from benchmarks.advection.backends import ArrayBackend  # noqa: E402
from benchmarks.advection.mlx_backend import MlxBackend  # noqa: E402

PARAMS = {
    "scheme": "muscl_vanleer",
    "resolutions": [64, 128, 256],
    "cfl": 0.5,
    "velocity": 1.0,
    "t_final": 0.5,
    "initial_condition": "square",
    "precision": "float32",
    "repeats": 1,
}


def runs_on(backend: ArrayBackend) -> list[dict[str, Any]]:
    resolutions: list[int] = PARAMS["resolutions"]  # type: ignore[assignment]
    stepper = solver.Stepper(backend, "muscl_vanleer", "float32", "array")
    return [solver.run_resolution(backend, stepper, PARAMS, nx, 1) for nx in resolutions]


def test_metal_matches_numpy_float32() -> None:
    for g, c in zip(runs_on(MlxBackend()), runs_on(ArrayBackend())):
        assert g["l2_error"] == pytest.approx(c["l2_error"], rel=1e-4)
        assert g["mass_error"] < 1e-5  # conservative in float32
        assert g["max_overshoot"] < 1e-5  # still TVD on the GPU


def test_metal_refuses_float64() -> None:
    import numpy as np

    with pytest.raises(ValueError, match="no float64"):
        MlxBackend().asarray(np.zeros(4), "float64")


def test_device_and_versions_are_recorded() -> None:
    be = MlxBackend()
    assert be.device().startswith("Apple")
    assert "mlx" in be.versions()
