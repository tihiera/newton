"""Periodic linear advection in 1D or 2D: a grid-refinement study ("convergence")
or a fixed-step speed measurement on large grids ("throughput"), for one scheme.

2D uses dimension splitting: an x sweep and a y sweep per time step, in
alternating order (xy, yx, ...), which keeps second-order schemes second order.
Every GPU result is checked against numpy at the same precision, on the same
machine, in the same job.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

import numpy as np

from . import bandwidth
from . import ir as ir_mod
from .backends import ArrayBackend, get_backend
from .power import gpu_activity, gpu_caveat, power_state, timing_caveat
from .schemes import FORMAL_ORDER, SCHEMES, step

GAUSS_NODES, GAUSS_WEIGHTS = np.polynomial.legendre.leggauss(8)
# Before timing: compile kernels, fill the memory pool, and keep the device busy
# long enough to reach its working clock (an idle GPU starts slow).
WARMUP_STEPS = 2
WARMUP_SECONDS = 0.25
# A grid this small stays in the GPU's cache between sweeps: its "bandwidth" is
# not the memory's, so no roofline fraction is given for it.
CACHE_RESIDENT_BYTES = 64 * 1024 * 1024
REFERENCE_STEPS = 10  # numpy steps for the speed comparison and the agreement check
# Largest relative difference from numpy that still counts as the same result.
AGREEMENT_TOLERANCE = {"float64": 1e-12, "float32": 1e-5}


def _pointwise(ic: str, x: np.ndarray) -> np.ndarray:
    x = np.mod(x, 1.0)
    if ic == "sine":
        return np.sin(2.0 * np.pi * x)
    if ic == "gaussian":
        return np.exp(-(((x - 0.5) / 0.08) ** 2))
    raise ValueError(ic)


def cell_averages(ic: str, nx: int, shift: float = 0.0) -> np.ndarray:
    """Exact (square) or 8-point Gauss–Legendre (smooth) cell averages of
    u0(x - shift) on [0, 1)."""
    dx = 1.0 / nx
    left = np.arange(nx) * dx - (shift % 1.0)  # periodic: whole periods change nothing
    if ic == "square":
        # u0 = 1 on [0.25, 0.75) (periodically), 0 elsewhere: exact overlap length.
        total = np.zeros(nx)
        for k in (-2, -1, 0, 1, 2):
            a, b = 0.25 + k, 0.75 + k
            total += np.clip(np.minimum(left + dx, b) - np.maximum(left, a), 0.0, None)
        return total / dx
    mid = left + 0.5 * dx
    pts = mid[:, None] + 0.5 * dx * GAUSS_NODES[None, :]
    return (_pointwise(ic, pts) * GAUSS_WEIGHTS[None, :]).sum(axis=1) * 0.5


def initial_state(ic: str, n: int, dims: int, shift_x: float = 0.0, shift_y: float = 0.0) -> Any:
    """Cell averages on an n (1D) or n x n (2D, rows = y) grid. The 2D initial
    conditions are products of the 1D ones, so their averages are exact products."""
    ux = cell_averages(ic, n, shift_x)
    if dims == 1:
        return ux
    return np.outer(cell_averages(ic, n, shift_y), ux)


class Stepper:
    """Advances the state one time step with one implementation: numpy-style array
    operations on the backend, or kernels: hand-written (engine E1, a scheme name)
    or generated from a SchemeIR document (engine E2, a dict)."""

    def __init__(self, be: ArrayBackend, scheme: str | dict[str, Any], precision: str,
                 implementation: str):  # fmt: skip
        self.be = be
        self.ir = scheme if isinstance(scheme, dict) else None
        self.flux = ir_mod.numpy_flux(self.ir) if self.ir else SCHEMES[str(scheme)]
        self.tableau = (self.ir or {}).get("time", {}).get("tableau")
        self.implementation = implementation
        self.sweeps: Any = None
        if implementation == "kernel":
            from .kernels import CudaSweeps, MetalSweeps

            header = ir_mod.cpp_header(self.ir) if self.ir else None
            name = "ir" if self.ir else str(scheme)
            if be.name == "cuda":
                self.sweeps = CudaSweeps(be.xp, precision, name, header)
            elif be.name == "metal":
                self.sweeps = MetalSweeps(be.xp, name, header)
            if self.sweeps is not None:
                self.sweeps.ir = self.ir
            else:
                raise ValueError(
                    "the kernel implementation runs on cuda or metal; use implementation "
                    "'array' on the cpu backend"
                )
        self._spare: Any = None

    def advance(self, u: Any, n: int, courant: dict[int, float]) -> Any:
        axes = list(courant)  # 2D: x (axis 1) then y; reversed on odd steps
        if n % 2:
            axes.reverse()
        for axis in axes:
            u = self.sweep(u, courant[axis], axis)
        return u

    def sweep(self, u: Any, c: float, axis: int) -> Any:
        if self.tableau is None:
            return self.one_step(u, c, axis)
        # Runge-Kutta (explicit tableau): K_j = -c dF(v_j), v_i = u + sum a_ij K_j,
        # u_new = u + sum b_i K_i; each K from one flux-form sweep of the stage value.
        a, b = self.tableau["a"], self.tableau["b"]
        ks: list[Any] = []
        for i in range(len(b)):
            v = u
            for j in range(i):
                if a[i][j]:
                    v = v + a[i][j] * ks[j]
            ks.append(self.one_step(v, c, axis) - v)
        out = u
        for weight, k in zip(b, ks):
            if weight:
                out = out + weight * k
        return out

    def one_step(self, u: Any, c: float, axis: int) -> Any:
        if self.sweeps is None:
            return step(self.be.xp, u, c, self.flux, axis)
        if self.be.name == "cuda" and self.tableau is None:  # ping-pong two buffers
            if self._spare is None or self._spare.shape != u.shape or self._spare is u:
                self._spare = self.be.xp.empty_like(u)
            out = self.sweeps(u, self._spare, c, axis)
            self._spare = u
            return out
        if self.be.name == "cuda":  # RK keeps its stage values: a fresh buffer each
            return self.sweeps(u, self.be.xp.empty_like(u), c, axis)
        return self.sweeps(u, None, c, axis)


def scheme_of(params: dict[str, Any]) -> str | dict[str, Any]:
    """A scheme name (hand-written, E1) or a validated SchemeIR document (E2)."""
    if params["scheme"] == "ir":
        return ir_mod.validate(params["scheme_ir"])
    return str(params["scheme"])


def claims_of(params: dict[str, Any]) -> dict[str, Any]:
    """What the scheme claims: the IR's own claims, or the library's for a name."""
    scheme = scheme_of(params)
    doc = scheme if isinstance(scheme, dict) else ir_mod.LIBRARY.get(scheme)
    if doc is None:
        return {"order": FORMAL_ORDER[str(scheme)], "max_cfl": 1.0, "tvd": False}
    claims: dict[str, Any] = ir_mod.validate(doc)["claims"]
    return claims


STABILITY_CELLS = 128
STABILITY_TRANSITS = 4  # the data crosses the grid this many times, whatever c is
STABILITY_MAX_STEPS = 50_000


def stability_probe(scheme: str | dict[str, Any], cfl: float) -> dict[str, Any]:
    """Is the scheme stable at this Courant number (and below it)? An unlimited
    scheme is linear: its von Neumann amplification factor is computed exactly, for
    Courant numbers up to cfl. A limited one is probed: rough data (every wavelength
    at once) carried several times across a small grid; unstable modes grow without
    bound, stable ones never exceed the data's range by much."""
    doc = scheme if isinstance(scheme, dict) else ir_mod.LIBRARY.get(str(scheme))
    if doc is not None and ir_mod.validate(doc)["flux"]["limiter"] == "none":
        worst = max(ir_mod.von_neumann(doc, cfl * i / 20) for i in range(1, 21))
        return {"cfl": cfl, "growth": worst, "stable": worst <= 1.0 + 1e-9,
                "method": "von Neumann (exact)"}  # fmt: skip
    u0 = np.random.default_rng(0).uniform(-1.0, 1.0, STABILITY_CELLS)
    stepper = Stepper(ArrayBackend(), scheme, "float64", "array")
    u = u0.copy()
    steps = min(STABILITY_MAX_STEPS, math.ceil(STABILITY_TRANSITS * STABILITY_CELLS / cfl))
    for k in range(steps):
        u = stepper.advance(u, k, {0: cfl})
        if not np.all(np.isfinite(u)):
            break
    growth = float(np.max(np.abs(u)) / np.max(np.abs(u0))) if np.all(np.isfinite(u)) else math.inf
    return {"cfl": cfl, "growth": growth, "stable": growth <= 2.0,
            "method": f"probe, {steps} steps"}  # fmt: skip


def assumptions(params: dict[str, Any], stepper: Stepper, runs: list[dict[str, Any]],
                metrics: dict[str, Any], reference: dict[str, Any] | None, precision: str,
                dims: int, order_fit: dict[str, Any] | None = None
                ) -> list[dict[str, Any]]:  # fmt: skip
    """Each claim of the scheme, against what this run measured. `holds` None: this
    run can't tell (e.g. order in throughput mode)."""
    claims = claims_of(params)
    scheme = scheme_of(params)
    tol = {"float64": 1e-12, "float32": 1e-5}[precision]
    out: list[dict[str, Any]] = []
    # Judged on the finest usable pair of grids (coarse grids aren't asymptotic yet),
    # and not at all on discontinuous data, where no scheme reaches its formal order.
    pairs = (order_fit or {}).get("pairwise") or []
    usable = (order_fit or {}).get("finest_usable")
    kept = [r["nx"] for r in runs if usable and r["nx"] <= usable]
    order = pairs[len(kept) - 2] if len(kept) >= 2 and len(pairs) >= len(kept) - 1 else None
    if params.get("initial_condition") == "square":
        order = None
    # Limiters clip at smooth extrema (by design): their measured order on smooth data
    # sits below the formal one (MUSCL-minmod ~1.65). Allow for that, and say so.
    doc = scheme if isinstance(scheme, dict) else ir_mod.LIBRARY.get(str(scheme), {})
    limited = (doc.get("flux") or {}).get("limiter", "none") != "none"
    allowance = 0.4 if limited else 0.25
    holds = None if order is None else order >= claims["order"] - allowance
    out.append({"claim": "order", "claimed": claims["order"], "measured": order,
                "allowance": allowance, "holds": holds})  # fmt: skip
    mass = max(abs(r["mass_error"]) for r in runs)
    out.append({"claim": "conservation", "claimed": True, "measured": mass,
                "holds": mass <= tol * 100})  # fmt: skip
    if "tv_increase" in metrics:
        tv = metrics["tv_increase"]
        scale = max(r.get("tv_initial") or 1.0 for r in runs)
        out.append({"claim": "tvd", "claimed": claims["tvd"], "measured": tv,
                    "holds": (tv <= tol * 100 * scale) if claims["tvd"] else None})  # fmt: skip
    probe = stability_probe(scheme, claims["max_cfl"])
    beyond = stability_probe(scheme, min(2.0, claims["max_cfl"] * 1.25))
    out.append({"claim": "max_cfl", "claimed": claims["max_cfl"],
                "measured": {"at_claim": probe, "beyond": beyond},
                "holds": probe["stable"]})  # fmt: skip
    if reference is not None:
        out.append({"claim": "agrees_with_numpy", "claimed": True,
                    "measured": reference["max_relative_difference"],
                    "holds": bool(reference["agrees"])})  # fmt: skip
    out.append(
        {
            "claim": "deterministic",
            "claimed": True,
            "measured": None,
            "holds": deterministic(stepper, params, precision, dims),
        }
    )
    return out


def deterministic(stepper: Stepper, params: dict[str, Any], precision: str, dims: int) -> bool:
    """The same steps twice from the same state: bit for bit the same answer."""
    n = min(int(params["resolutions"][-1]), 256 if dims == 2 else 4096)
    courant = courant_numbers(params, n, step_count(params, n, dims), dims)
    results = []
    for _ in range(2):
        u = stepper.be.asarray(initial_state(params["initial_condition"], n, dims), precision)
        for k in range(REFERENCE_STEPS):
            u = stepper.advance(u, k, courant)
        results.append(stepper.be.to_host(u))
    return bool(np.array_equal(results[0], results[1]))


def courant_numbers(params: dict[str, Any], n: int, steps: int, dims: int) -> dict[int, float]:
    dx = 1.0 / n
    dt = float(params["t_final"]) / steps
    if dims == 1:
        return {0: float(params["velocity"]) * dt / dx}
    return {1: float(params["velocity"]) * dt / dx, 0: float(params["velocity_y"]) * dt / dx}


def step_count(params: dict[str, Any], n: int, dims: int, coarsest: int | None = None) -> int:
    """Time steps to reach t_final on an n grid with Courant number <= cfl.

    In a refinement study every grid uses the coarsest grid's Courant number: error
    constants depend on it ((1 - c) for upwind), and a Courant number that drifts
    from grid to grid (as rounding each grid's step count up would make it) shows
    up as a false change of order."""
    speed = float(params["velocity"])
    if dims == 2:
        speed = max(speed, float(params["velocity_y"]))
    distance = float(params["t_final"]) * speed  # in cell widths: times n
    base = coarsest or n
    base_steps = max(1, math.ceil(distance * base / float(params["cfl"]) - 1e-9))
    if base == n:
        return base_steps
    c = distance * base / base_steps
    return max(1, math.ceil(distance * n / c - 1e-9))


def total_variation(xp: Any, u: Any) -> Any:
    tv = xp.abs(xp.roll(u, -1, axis=0) - u).sum()
    if u.ndim == 2:
        tv = tv + xp.abs(xp.roll(u, -1, axis=1) - u).sum()
    return tv


def _warm_up(stepper: Stepper, u0: Any, courant: dict[int, float]) -> None:
    u = u0
    t0 = time.perf_counter()
    n = 0
    while n < WARMUP_STEPS or (time.perf_counter() - t0 < WARMUP_SECONDS and n < 1000):
        u = stepper.advance(u, n, courant)
        stepper.be.checkpoint(n, u)
        if n % 8 == 7:
            stepper.be.sync(u)
        n += 1
    stepper.be.sync(u)


def run_resolution(
    be: ArrayBackend, stepper: Stepper, params: dict[str, Any], n: int, dims: int
) -> dict[str, Any]:
    """One grid of the refinement study, with the per-step diagnostics (total
    variation, extrema) that the evidence checks need."""
    xp = be.xp
    precision = params.get("precision", "float64")
    steps = step_count(params, n, dims, min(int(r) for r in params["resolutions"]))
    courant = courant_numbers(params, n, steps, dims)
    ic = params["initial_condition"]
    t_final = float(params["t_final"])
    u0_host = initial_state(ic, n, dims)
    exact = initial_state(
        ic, n, dims, float(params["velocity"]) * t_final,
        float(params.get("velocity_y", 0.0)) * t_final,
    )  # fmt: skip
    cells = n**dims
    be.set_problem_size(cells, np.dtype(precision).itemsize)
    _warm_up(stepper, be.asarray(u0_host, precision), courant)

    best_runtime = math.inf
    u: Any = None
    tv_max: Any = None
    hi: Any = None
    lo: Any = None
    for _ in range(int(params.get("repeats", 1))):
        u = be.asarray(u0_host, precision)
        tv_max = total_variation(xp, u)
        hi, lo = u.max(), u.min()
        be.sync(u, tv_max, hi, lo)
        t0 = time.perf_counter()
        for k in range(steps):
            u = stepper.advance(u, k, courant)
            tv_max = xp.maximum(tv_max, total_variation(xp, u))
            hi = xp.maximum(hi, u.max())
            lo = xp.minimum(lo, u.min())
            be.checkpoint(k, u, tv_max, hi, lo)
        be.sync(u, tv_max, hi, lo)
        best_runtime = min(best_runtime, time.perf_counter() - t0)

    u_host = be.to_host(u)
    err = u_host - exact
    cell_volume = 1.0 / cells
    tv0 = float(total_variation(np, u0_host))
    finite = bool(np.all(np.isfinite(u_host)))
    bound = 10.0 * max(1e-12, float(np.abs(u0_host).max()))
    return {
        "nx": n,
        "dims": dims,
        "cells": cells,
        "dx": 1.0 / n,
        "dt": t_final / steps,
        "courant": max(courant.values()),
        "steps": steps,
        "l2_error": float(np.sqrt(cell_volume * np.sum(err**2))) if finite else math.inf,
        "linf_error": float(np.max(np.abs(err))) if finite else math.inf,
        "mass_error": float(abs(u_host.sum() - u0_host.sum()) * cell_volume)
        if finite
        else math.inf,
        "tv_initial": tv0,
        "tv_increase": float(tv_max) - tv0,
        "max_overshoot": max(
            0.0, float(hi) - float(u0_host.max()), float(u0_host.min()) - float(lo)
        ),
        "runtime_s": best_runtime,
        "cell_updates_per_s": cells * dims * steps / best_runtime if best_runtime > 0 else None,
        "stable": finite and float(np.abs(u_host).max()) <= bound,
        "_solution": u_host,
        # The final state's exact bits: two implementations agree bit for bit iff equal.
        "solution_sha256": hashlib.sha256(np.ascontiguousarray(u_host).tobytes()).hexdigest(),
        "_exact": exact,
    }


def run_throughput(
    be: ArrayBackend, stepper: Stepper, params: dict[str, Any], n: int, dims: int
) -> dict[str, Any]:
    """A fixed number of steps on one large grid, timed without diagnostics: the
    speed of the scheme itself. Conservation, extrema and finiteness are checked
    on the final state."""
    precision = params.get("precision", "float64")
    steps = int(params["steps"])
    courant = courant_numbers(params, n, step_count(params, n, dims), dims)
    u0_host = initial_state(params["initial_condition"], n, dims)
    cells = n**dims
    be.set_problem_size(cells, np.dtype(precision).itemsize)
    _warm_up(stepper, be.asarray(u0_host, precision), courant)
    best = math.inf
    u: Any = None
    for _ in range(int(params.get("repeats", 1))):
        u = be.asarray(u0_host, precision)
        be.sync(u)
        t0 = time.perf_counter()
        for k in range(steps):
            u = stepper.advance(u, k, courant)
            be.checkpoint(k, u)
        be.sync(u)
        best = min(best, time.perf_counter() - t0)
    u_host = be.to_host(u)
    finite = bool(np.all(np.isfinite(u_host)))
    return {
        "nx": n,
        "dims": dims,
        "cells": cells,
        "courant": max(courant.values()),
        "steps": steps,
        "mass_error": float(abs(u_host.sum() - u0_host.sum()) / cells) if finite else math.inf,
        "max_overshoot": max(
            0.0,
            float(u_host.max()) - float(u0_host.max()),
            float(u0_host.min()) - float(u_host.min()),
        )
        if finite
        else math.inf,
        "runtime_s": best,
        "time_per_step_s": best / steps,
        "cell_updates_per_s": cells * dims * steps / best if best > 0 else None,
        "stable": finite and float(np.abs(u_host).max()) <= 10.0 * float(np.abs(u0_host).max()),
        "_solution": u_host,
        # The final state's exact bits: two implementations agree bit for bit iff equal.
        "solution_sha256": hashlib.sha256(np.ascontiguousarray(u_host).tobytes()).hexdigest(),
    }


def merge_activity(*samples: dict[str, Any] | None) -> dict[str, Any] | None:
    seen = [s for s in samples if s]
    if not seen:
        return None
    busy = [s["utilization_pct"] for s in seen if s.get("utilization_pct") is not None]
    return {
        "utilization_pct": max(busy) if busy else None,
        "other_processes": max(s.get("other_processes") or 0 for s in seen),
    }


def numpy_reference(
    be: ArrayBackend, stepper: Stepper, params: dict[str, Any], n: int, dims: int
) -> dict[str, Any]:
    """The same steps with numpy at the same precision, on this machine's CPU:
    how much faster the backend is, and whether it computes the same numbers."""
    precision = params.get("precision", "float64")
    steps = min(REFERENCE_STEPS, int(params.get("steps") or step_count(params, n, dims)))
    courant = courant_numbers(params, n, step_count(params, n, dims), dims)
    u0_host = initial_state(params["initial_condition"], n, dims)
    cpu = Stepper(ArrayBackend(), scheme_of(params), precision, "array")

    cpu.advance(np.asarray(u0_host, dtype=precision), 0, courant)  # warm-up, untimed
    numpy_per_step = math.inf
    ref: Any = None
    for _ in range(int(params.get("repeats", 1))):  # best of N, like the backend's timing
        ref = np.asarray(u0_host, dtype=precision)
        t0 = time.perf_counter()
        for k in range(steps):
            ref = cpu.advance(ref, k, courant)
        numpy_per_step = min(numpy_per_step, (time.perf_counter() - t0) / steps)

    # The backend's same steps, untimed (its speed is measured warm, elsewhere).
    u = be.asarray(u0_host, precision)
    for k in range(steps):
        u = stepper.advance(u, k, courant)
        be.checkpoint(k, u)
    be.sync(u)
    got = be.to_host(u)
    scale = max(float(np.abs(ref).max()), 1e-300)
    diff = float(np.abs(got - ref.astype(np.float64)).max()) / scale
    return {
        "steps": steps,
        "numpy_time_per_step_s": numpy_per_step,
        "max_relative_difference": diff,
        "tolerance": AGREEMENT_TOLERANCE[precision],
        "agrees": bool(np.isfinite(diff)) and diff <= AGREEMENT_TOLERANCE[precision],
    }


# Round-off cut-off for the order fit, chosen on measured float32 vs float64
# refinements (10 cases, 4 schemes, cfl 0.4-1.0, 64-16384 cells): the float32 fit
# stays within 0.04 of the float64 order, and no float64 grid is ever dropped.
ROUNDOFF_FLOOR = 3.0  # x accumulated rounding, eps * sqrt(sweeps)
ROUNDOFF_NEAR = 300.0  # within this factor, an odd order means rounding took over
ORDER_DROP = 0.6  # "odd": below formal - this, above formal + 1.5, or negative


def observed_order(
    runs: list[dict[str, Any]], precision: str = "float64", formal: float | None = None
) -> dict[str, Any]:
    """Least-squares slope of log(L2) vs log(dx), plus pairwise orders.

    Fine grids in float32 can stop measuring the scheme: its per-step correction
    falls below the precision and is rounded away (the error then collapses, at
    orders of 4-8) or rounding noise piles up over the many steps (the error stops
    falling or grows). Walking from coarse to fine, the first grid whose error is
    at the rounding level, or near it with an odd order, ends the fit: it and every
    finer grid are reported as round-off limited. Too few grids left: no fit."""
    eps = float(np.finfo(precision).eps)
    ordered = sorted(runs, key=lambda r: r["nx"])
    kept: list[dict[str, Any]] = []
    for r in ordered:
        err = r["l2_error"]
        if not (r["stable"] and 0 < err < math.inf):
            continue
        noise = eps * math.sqrt(r["steps"] * r.get("dims", 1))
        if err < ROUNDOFF_FLOOR * noise:
            break
        if kept and formal is not None and err < ROUNDOFF_NEAR * noise:
            prev = kept[-1]
            order = math.log(prev["l2_error"] / err) / math.log(prev["dx"] / r["dx"])
            if order < 0 or order > formal + 1.5 or order < formal - ORDER_DROP:
                break
        kept.append(r)
    cutoff = ordered.index(kept[-1]) + 1 if kept else 0
    limited = [r["nx"] for r in ordered[cutoff:]]
    pairwise = []
    for r1, r2 in zip(ordered, ordered[1:]):
        e1, e2 = r1["l2_error"], r2["l2_error"]
        if 0 < e1 < math.inf and 0 < e2 < math.inf:
            pairwise.append(math.log(e1 / e2) / math.log(r1["dx"] / r2["dx"]))
    out: dict[str, Any] = {"fit": None, "pairwise": pairwise, "roundoff_limited": limited,
                           "finest_usable": kept[-1]["nx"] if kept else None}  # fmt: skip
    pts = [(math.log(r["dx"]), math.log(r["l2_error"])) for r in kept if r["l2_error"] > 1e-14]
    if len(pts) >= 2:
        xs, ys = zip(*pts)
        out["fit"] = float(np.polyfit(xs, ys, 1)[0])
    return out


def run_study(params: dict[str, Any], backend: str) -> dict[str, Any]:
    """Raises BackendUnavailable when the backend can't run here (never falls back)."""
    be = get_backend(backend)
    precision = params.get("precision", "float64")
    if precision not in be.precisions:
        raise ValueError(f"{be.name} does not support {precision}")
    dims = int(params.get("dims", 1))
    mode = params.get("mode", "convergence")
    implementation = params.get("implementation", "array")
    stepper = Stepper(be, scheme_of(params), precision, implementation)
    itemsize = np.dtype(precision).itemsize
    compare = not (be.name == "cpu" and implementation == "array")  # numpy vs itself
    # Sampled before the job loads the GPU, and again after: on a GB10 the CPU and
    # GPU share one memory bus, so another process's GPU work slows cpu runs too.
    before = gpu_activity()
    try:
        try:
            roof = bandwidth.probe(be, precision)
        except Exception as e:  # the roof is extra information: never fail a run on it
            roof = {"error": f"{type(e).__name__}: {e}", "peak_gbps": None,
                    "copy_gbps": None, "triad_gbps": None}  # fmt: skip
        if mode == "throughput":
            n = int(params["resolutions"][-1])
            runs = [run_throughput(be, stepper, params, n, dims)]
            reference = numpy_reference(be, stepper, params, n, dims) if compare else None
        else:
            runs = [
                run_resolution(be, stepper, params, n, dims) for n in sorted(params["resolutions"])
            ]
            # Agreement on the coarsest grid; speed is measured in throughput mode.
            reference = (
                numpy_reference(be, stepper, params, runs[0]["nx"], dims) if compare else None
            )
    finally:
        be.release()
    activity = merge_activity(before, gpu_activity())

    finest = runs[-1]
    metrics: dict[str, Any] = {
        "runtime": sum(r["runtime_s"] for r in runs),
        "runtime_finest": finest["runtime_s"],
        "cell_updates_per_s": finest["cell_updates_per_s"],
        "peak_gbps": roof["peak_gbps"],
        "conservation": max(r["mass_error"] for r in runs),
        "max_overshoot": max(r["max_overshoot"] for r in runs),
        "stable": all(r["stable"] for r in runs),
        # None: nothing to compare with (numpy compared with itself).
        "reference_agreement": reference["max_relative_difference"] if reference else None,
        "agrees_with_numpy": reference["agrees"] if reference else None,
    }
    order = None
    if mode == "throughput":
        # Bandwidth only from runs timed without diagnostics, and only for grids too
        # big for the cache (or the "memory" bandwidth would be the cache's).
        gbps = bandwidth.achieved(
            finest["cells"] * dims * finest["steps"], itemsize, finest["runtime_s"]
        )
        cache_resident = 2 * finest["cells"] * itemsize < CACHE_RESIDENT_BYTES
        metrics.update(
            time_per_step=finest["time_per_step_s"],
            speedup_vs_numpy=(
                reference["numpy_time_per_step_s"] / finest["time_per_step_s"]
                if reference
                else None
            ),
            achieved_gbps=gbps,
            cache_resident=cache_resident,
            bandwidth_fraction=(
                gbps / roof["peak_gbps"] if roof["peak_gbps"] and not cache_resident else None
            ),
        )
    else:
        # The round-off cut-off needs an expected order: never more than 2 for these
        # three-point schemes, so an overclaimed order can't switch its own check off.
        order = observed_order(runs, precision, min(2, claims_of(params)["order"]))
        # Accuracy from the finest grid the precision can resolve (a float32 grid past
        # the round-off cut-off has a meaningless, often tiny, error).
        usable = next((r for r in runs if r["nx"] == order["finest_usable"]), None)
        metrics.update(
            l2_error=usable["l2_error"] if usable else None,
            linf_error=usable["linf_error"] if usable else None,
            l2_grid=usable["nx"] if usable else None,
            observed_order=order["fit"],
            tv_increase=max(r["tv_increase"] for r in runs),
        )
    env: dict[str, Any] = {"backend": be.name, "device": be.device(), **be.versions()}
    if implementation == "kernel":
        from .kernels import hashes

        if stepper.ir is not None:  # generated (E2): its exact text, by hash
            env["kernels"] = {
                "advect.cu": hashes()["advect.cu"], "advect.metal": hashes()["advect.metal"],
                f"ir:{stepper.ir['name']}.h": ir_mod.header_hash(stepper.ir),
            }  # fmt: skip
        else:
            env["kernels"] = hashes()
    if stepper.ir is not None:
        env["scheme_ir"] = ir_mod.digest(stepper.ir)
    power = power_state()
    if power:
        env["power"] = power
    if activity:
        env["gpu_activity"] = activity
    caveat = timing_caveat(power) or gpu_caveat(activity)
    if caveat:
        env["timing_caveat"] = caveat
    return {
        "metrics": metrics,
        "observed_order": order,
        "runs": runs,
        "bandwidth": roof,
        "reference": reference,
        "environment": env,
        "assumptions": assumptions(
            params, stepper, runs, metrics, reference, precision, dims, order
        ),
    }
