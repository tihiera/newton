"""Backend registry: strict names, and the CUDA path exercised with a numpy-backed
fake CuPy (so it runs on machines without an NVIDIA GPU)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from benchmarks.advection.backends import BackendUnavailable, canonical, get_backend
from benchmarks.advection.solver import run_study

PARAMS = {
    "scheme": "lax_wendroff",
    "resolutions": [32, 64],
    "cfl": 0.5,
    "velocity": 1.0,
    "t_final": 0.5,
    "initial_condition": "sine",
    "precision": "float64",
    "repeats": 1,
}


class FakeDeviceArray(np.ndarray):
    def get(self) -> np.ndarray:
        return np.asarray(self)


def fake_cupy(device_count: int = 1, broken: bool = False) -> types.ModuleType:
    def wrap(fn: Any) -> Any:
        def inner(*a: Any, **k: Any) -> Any:
            if broken:
                raise RuntimeError("NVRTC_ERROR_INVALID_OPTION: --gpu-architecture=sm_121")
            return np.asarray(fn(*a, **k)).view(FakeDeviceArray)

        return inner

    cp = types.ModuleType("cupy")
    cp.__version__ = "14.2.0-fake"  # type: ignore[attr-defined]
    for name in ("arange", "where", "roll", "abs", "maximum", "minimum", "asarray"):
        setattr(cp, name, wrap(getattr(np, name)))
    cp.float64, cp.float32 = np.float64, np.float32  # type: ignore[attr-defined]
    runtime = types.SimpleNamespace(
        getDeviceCount=lambda: device_count,
        getDeviceProperties=lambda i: {"name": b"NVIDIA GB10"},
    )
    cp.cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        runtime=runtime, Device=lambda: types.SimpleNamespace(synchronize=lambda: None)
    )
    cp.get_default_memory_pool = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        free_all_blocks=lambda: None
    )
    return cp


def test_names_are_strict() -> None:
    assert canonical("gpu") == "cuda"
    with pytest.raises(ValueError, match="unknown backend"):
        canonical("tpu")


def test_cuda_path_matches_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cupy", fake_cupy())
    cpu = run_study(PARAMS, "cpu")
    cuda = run_study(PARAMS, "cuda")
    assert cuda["environment"]["backend"] == "cuda"
    assert cuda["environment"]["device"] == "NVIDIA GB10"
    assert cuda["environment"]["cupy"] == "14.2.0-fake"
    assert cuda["metrics"]["l2_error"] == pytest.approx(cpu["metrics"]["l2_error"], rel=1e-12)


def test_cuda_unavailable_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cupy", None)  # import fails
    with pytest.raises(BackendUnavailable, match="CuPy is not installed"):
        get_backend("cuda")
    monkeypatch.setitem(sys.modules, "cupy", fake_cupy(device_count=0))
    with pytest.raises(BackendUnavailable, match="no CUDA device"):
        get_backend("cuda")
    monkeypatch.setitem(sys.modules, "cupy", fake_cupy(broken=True))
    with pytest.raises(BackendUnavailable, match="NVRTC"):
        get_backend("cuda")


def test_cli_fails_loudly_instead_of_falling_back(tmp_path: Path) -> None:
    if importlib.util.find_spec("cupy") is not None:
        pytest.skip("CuPy is installed here, so cuda may genuinely work")
    params = tmp_path / "params.json"
    params.write_text(json.dumps(PARAMS))
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.advection", "--params", str(params), "--out",
         str(tmp_path / "out")],
        cwd=root,
        env={"PATH": "/usr/bin:/bin", "NEWTON_BACKEND": "cuda", "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=120,
    )  # fmt: skip
    assert proc.returncode == 2
    results = json.loads((tmp_path / "out" / "results.json").read_text())
    assert results["backend"] == "cuda"
    assert "CuPy is not installed" in results["error"]
    assert "runs" not in results  # nothing was silently computed on the CPU
