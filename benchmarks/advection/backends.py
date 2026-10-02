"""Array backends for the advection benchmark: one strict registry.

An unknown backend name is an error, never a silent fallback to numpy: a run
that claims "cuda" must have run on CUDA.
"""

from __future__ import annotations

import platform
from typing import Any, Callable

import numpy as np

LEGACY_ALIASES = {"gpu": "cuda"}


class BackendUnavailable(RuntimeError):
    """The requested backend can't run on this machine (message says why)."""


class ArrayBackend:
    name = "cpu"
    precisions: tuple[str, ...] = ("float64", "float32")

    def __init__(self) -> None:
        self.xp: Any = np

    def asarray(self, host: np.ndarray, precision: str) -> Any:
        return self.xp.asarray(host, dtype=getattr(np, precision))

    def to_host(self, arr: Any) -> np.ndarray:
        return np.asarray(arr, dtype=np.float64)

    def sync(self, *arrays: Any) -> None:
        """Block until all queued work on the device is done (timing boundary)."""

    def checkpoint(self, step: int, *arrays: Any) -> None:
        """Called every time step; lazy backends evaluate here periodically."""

    def set_problem_size(self, cells: int, itemsize: int = 8) -> None:
        """Called before each grid; lazy backends size their evaluation batches."""

    def device(self) -> str:
        return f"cpu ({platform.processor() or platform.machine()})"

    def versions(self) -> dict[str, str]:
        return {"numpy": np.__version__}

    def release(self) -> None:
        """Give cached device memory back (shared with the CPU on unified-memory GPUs)."""


class CupyBackend(ArrayBackend):
    name = "cuda"

    def __init__(self) -> None:
        super().__init__()
        try:
            import cupy
        except ImportError as e:
            raise BackendUnavailable(
                "cuda backend requested but CuPy is not installed on this host "
                "(run 'Install GPU support' for the host)"
            ) from e
        try:
            if cupy.cuda.runtime.getDeviceCount() < 1:
                raise BackendUnavailable("cuda backend requested but no CUDA device is visible")
            # Smoke kernel: NVRTC compile + every op the schemes use + a sync. A broken
            # toolkit (e.g. NVRTC too old for the GPU) fails here with a clear message
            # instead of mid-run.
            a = cupy.arange(16, dtype=cupy.float64)
            b = cupy.where(cupy.roll(a, 1) > a, cupy.abs(a), cupy.maximum(a, 1.0))
            float(b.sum())
            cupy.cuda.Device().synchronize()
        except BackendUnavailable:
            raise
        except Exception as e:
            raise BackendUnavailable(
                f"cuda backend unusable on this host: {type(e).__name__}: {e}"
            ) from e
        self.xp = cupy

    def to_host(self, arr: Any) -> np.ndarray:
        return np.asarray(arr.get(), dtype=np.float64)

    def sync(self, *arrays: Any) -> None:
        self.xp.cuda.Device().synchronize()

    def device(self) -> str:
        props = self.xp.cuda.runtime.getDeviceProperties(0)
        name = props["name"]
        return str(name.decode() if isinstance(name, bytes) else name)

    def versions(self) -> dict[str, str]:
        return {"numpy": np.__version__, "cupy": str(self.xp.__version__)}

    def release(self) -> None:
        self.xp.get_default_memory_pool().free_all_blocks()


def _metal() -> ArrayBackend:
    from .mlx_backend import MlxBackend  # MLX exists only on Apple Silicon

    return MlxBackend()


REGISTRY: dict[str, Callable[[], ArrayBackend]] = {
    "cpu": ArrayBackend,
    "cuda": CupyBackend,
    "metal": _metal,
}


def canonical(name: str) -> str:
    name = LEGACY_ALIASES.get(name, name)
    if name not in REGISTRY:
        raise ValueError(f"unknown backend {name!r}; expected one of {sorted(REGISTRY)}")
    return name


def get_backend(name: str) -> ArrayBackend:
    return REGISTRY[canonical(name)]()


def names() -> list[str]:
    return sorted(REGISTRY)
