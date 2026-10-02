"""Hand-written sweep kernels (engine E1): CUDA via CuPy's RawModule, Metal via
MLX's metal_kernel. Both compile kernels/advect.h, the numpy formulas of
schemes.py written once in C++. Their sha256 hashes go into every result, so a
report says exactly which kernel code produced it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from .schemes import SCHEME_IDS

KERNEL_DIR = Path(__file__).resolve().parent / "kernels"
KERNEL_FILES = ("advect.h", "advect.cu", "advect.metal")
THREADS = 256


def source(name: str) -> str:
    return (KERNEL_DIR / name).read_text()


def hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((KERNEL_DIR / name).read_bytes()).hexdigest() for name in KERNEL_FILES
    }


def coefficients(c: float, ir: dict[str, Any] | None = None) -> tuple[float, float]:
    """(c, k) with k = 0.5 * (1 - c) in double precision, as numpy computes it; for a
    SchemeIR, k = A(c) evaluated as written (upwind: k is unused)."""
    if ir is not None:
        from .ir import coefficient

        k = coefficient(ir, c)
        return c, 0.0 if k is None else k
    return c, 0.5 * (1.0 - c)


class CudaSweeps:
    """advect_sweep<T, SCHEME, AXIS> for one dtype and scheme, both axes. `header`:
    a generated flux (engine E2, ir.cpp_header) in place of advect.h."""

    def __init__(self, cupy: Any, dtype: Any, scheme: str, header: str | None = None) -> None:
        self.cp = cupy
        self.dtype = np.dtype(dtype)
        ctype = {"float64": "double", "float32": "float"}[self.dtype.name]
        sid = 0 if header is not None else SCHEME_IDS[scheme]
        names = [f"advect_sweep<{ctype}, {sid}, {axis}>" for axis in (0, 1)]
        code = (
            "#define NEWTON_FN __device__ __forceinline__\n"
            + (header if header is not None else source("advect.h"))
            + source("advect.cu")
        )
        module = cupy.RawModule(
            code=code, options=("--std=c++14", "--fmad=false"), name_expressions=names
        )
        self.kernels = {axis: module.get_function(name) for axis, name in zip((0, 1), names)}
        self.ir: dict[str, Any] | None = None  # set with a generated header

    def __call__(self, u: Any, out: Any, c: float, axis: int) -> Any:
        ny, nx = (1, u.shape[0]) if u.ndim == 1 else u.shape
        cc, k = coefficients(c, self.ir)
        t = self.dtype.type
        blocks = (u.size + THREADS - 1) // THREADS
        self.kernels[axis if u.ndim == 2 else 1](
            (blocks,), (THREADS,), (u, out, t(cc), t(k), np.int32(nx), np.int32(ny))
        )
        return out


class MetalSweeps:
    """The same sweep as a Metal kernel (float32: Apple GPUs have no float64)."""

    def __init__(self, mx: Any, scheme: str, header: str | None = None) -> None:
        self.mx = mx
        self.scheme = 0 if header is not None else SCHEME_IDS[scheme]
        self.kernel = mx.fast.metal_kernel(
            name="newton_advect_sweep" if header is None else "newton_advect_ir_sweep",
            input_names=["u", "coef"],
            output_names=["out"],
            header="#define NEWTON_FN inline\n"
            + (header if header is not None else source("advect.h")),
            source=source("advect.metal"),
        )
        self.ir: dict[str, Any] | None = None  # set with a generated header

    def __call__(self, u: Any, out: Any, c: float, axis: int) -> Any:
        mx = self.mx
        ny, nx = (1, u.shape[0]) if u.ndim == 1 else u.shape
        coef = mx.array(np.asarray(coefficients(c, self.ir), dtype=np.float32))
        (result,) = self.kernel(
            inputs=[u, coef],
            template=[("T", mx.float32), ("SCHEME", self.scheme),
                      ("AXIS", axis if u.ndim == 2 else 1), ("NX", nx), ("NY", ny)],
            grid=(u.size, 1, 1),
            threadgroup=(THREADS, 1, 1),
            output_shapes=[u.shape],
            output_dtypes=[u.dtype],
        )  # fmt: skip
        return result
