"""Measured memory bandwidth of the device a job runs on (the roofline's roof).

Advection sweeps do a handful of flops per value moved, so they are memory-bound
on every device here: the honest "how fast could this be" is the bandwidth the
same device reaches on a plain copy and a STREAM triad (a = b + s*c), measured in
the same job, not a datasheet number.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from .backends import ArrayBackend

PROBE_BYTES = 256 * 1024 * 1024  # per array: well past every cache
REPEATS = 5


def probe(be: ArrayBackend, precision: str) -> dict[str, Any]:
    dtype = np.dtype(precision)
    n = PROBE_BYTES // dtype.itemsize
    xp = be.xp
    host = np.ones(n, dtype=dtype)
    a = be.asarray(host, precision)
    b = be.asarray(host * 2, precision)
    c = be.asarray(host * 3, precision)
    del host
    copy, triad = _ops(be, a, b, c)
    out: dict[str, Any] = {"array_bytes": n * dtype.itemsize}
    # Lazy backends (MLX) pay a round trip per evaluation: chain several operations
    # into each one, so the probe measures memory, not launch overhead. Chained, not
    # independent: independent copies of one array run together and share its reads
    # through the cache, which overstates the bandwidth (measured: 360 GB/s on a
    # 300 GB/s M3 Max).
    chain = 8 if be.name == "metal" else 1
    for name, fn, arrays in (("copy", copy, 2), ("triad", triad, 3)):
        best = float("inf")
        for i in range(REPEATS + 1):  # the first run is a warm-up (allocation, JIT)
            be.sync(a, b, c)
            t0 = time.perf_counter()
            result = fn(None)
            for _ in range(chain - 1):
                result = fn(result)
            be.sync(result)
            elapsed = (time.perf_counter() - t0) / chain
            del result
            if i:
                best = min(best, elapsed)
        out[f"{name}_gbps"] = arrays * n * dtype.itemsize / best / 1e9
    out["peak_gbps"] = max(out["copy_gbps"], out["triad_gbps"])
    del a, b, c, xp
    be.release()
    return out


def _ops(be: ArrayBackend, a: Any, b: Any, c: Any) -> tuple[Any, Any]:
    xp = be.xp
    if be.name == "cpu":
        tmp = np.empty_like(a)

        def copy_np(_: Any) -> Any:
            np.copyto(tmp, a)
            return tmp

        def triad_np(_: Any) -> Any:  # two passes in numpy: counted as one triad (a lower bound)
            np.multiply(c, 3.0, out=tmp)
            np.add(b, tmp, out=tmp)
            return tmp

        return copy_np, triad_np
    if be.name == "cuda":
        tmp = xp.empty_like(a)
        triad = xp.ElementwiseKernel("T b, T c, T s", "T a", "a = b + s * c", "newton_stream_triad")
        scalar = a.dtype.type(3.0)
        return (lambda _: (xp.copyto(tmp, a), tmp)[1]), (lambda _: triad(b, c, scalar, tmp))
    if be.name == "metal":
        fused = xp.compile(lambda b_, c_: b_ + 3.0 * c_)  # one kernel, like STREAM
        return (
            lambda prev: (a if prev is None else prev) + 0.0,
            lambda prev: fused(b if prev is None else prev, c),
        )
    raise ValueError(f"no bandwidth probe for backend {be.name}")


def achieved(cell_updates: float, itemsize: int, seconds: float) -> float:
    """GB/s at the minimum traffic of a sweep: each value read once and written once."""
    return 2 * itemsize * cell_updates / seconds / 1e9 if seconds > 0 else 0.0
