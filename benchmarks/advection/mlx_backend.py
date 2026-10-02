"""Apple GPU (Metal) backend via MLX.

MLX is lazy: operations only build a graph until something is evaluated. The
time-step loop therefore evaluates every `eval_every` steps (keeping the graph
small) and `sync` evaluates + waits before the timer is read, so timings measure
GPU work, not graph construction. Apple GPUs have no float64.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .backends import ArrayBackend, BackendUnavailable

# Evaluating every step costs ~0.2 ms of overhead; never evaluating grows the
# graph without bound. Measured on an M3 Max, 32-64 steps per eval is the sweet
# spot for the uncompiled 1D schemes.
EVAL_EVERY = 32


class MlxBackend(ArrayBackend):
    name = "metal"
    precisions = ("float32",)

    def __init__(self, eval_every: int = EVAL_EVERY) -> None:
        super().__init__()
        try:
            import mlx.core as mx
        except ImportError as e:
            raise BackendUnavailable(
                "metal backend requested but MLX is not installed on this machine"
            ) from e
        try:
            if not mx.metal.is_available():
                raise BackendUnavailable("metal backend requested but Metal is not available")
            a = mx.array(np.arange(16, dtype=np.float32))
            b = mx.where(mx.roll(a, 1) > a, mx.abs(a), mx.maximum(a, 1.0))
            float(b.sum())
        except BackendUnavailable:
            raise
        except Exception as e:
            raise BackendUnavailable(f"metal backend unusable: {type(e).__name__}: {e}") from e
        self.xp = mx
        self.eval_every = self.max_eval_every = max(1, int(eval_every))

    def asarray(self, host: np.ndarray, precision: str) -> Any:
        if precision != "float32":
            raise ValueError("Apple GPUs have no float64; use precision float32")
        return self.xp.array(np.asarray(host, dtype=np.float32))

    def to_host(self, arr: Any) -> np.ndarray:
        self.xp.eval(arr)
        return np.asarray(np.array(arr), dtype=np.float64)

    def sync(self, *arrays: Any) -> None:
        if arrays:
            self.xp.eval(*arrays)
        self.xp.synchronize()

    def set_problem_size(self, cells: int, itemsize: int = 4) -> None:
        # A lazy graph keeps every intermediate array until it is evaluated, and each
        # evaluation is a round trip that leaves the GPU idle: evaluate as rarely as
        # a ~512 MB budget of pending arrays allows.
        budget = (512 << 20) // max(1, 2 * cells * itemsize)
        self.eval_every = max(1, min(self.max_eval_every, budget))

    def checkpoint(self, step: int, *arrays: Any) -> None:
        if (step + 1) % self.eval_every == 0:
            self.xp.eval(*arrays)

    def device(self) -> str:
        info = self.xp.device_info()
        return str(info.get("device_name") or "Apple GPU")

    def versions(self) -> dict[str, str]:
        return {
            "numpy": np.__version__,
            "mlx": str(self.xp.__version__),
            "eval_every": str(self.eval_every),
        }

    def release(self) -> None:
        self.xp.clear_cache()
