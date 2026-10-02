"""How long an experiment will take, and whether it fits in memory, for the
approval. Time: the work (cell updates) of each variant divided by a rate. The
rate is the median this host reached on comparable past runs (same backend,
implementation, mode, dimensions and precision, and a grid within 16x of this
one's: small grids are launch-bound and far slower per cell), or a rough default
until it has such history. Timeouts are per job, so the slowest job is what is
compared with the timeout; the total is shown as the expected wall-clock time."""

from __future__ import annotations

import math
import statistics
from typing import Any

from ..contracts import AdvectionParams, ExperimentSpec
from ..storage.db import Database, loads

# Cell updates per second, measured on a GB10 and an M3 Max (2026-10, unloaded).
# Convergence runs also compute diagnostics every step, about 3x the work.
DEFAULT_RATES = {
    ("cpu", "array"): 9e7,
    ("cuda", "array"): 3e8,
    ("cuda", "kernel"): 3e9,
    ("metal", "array"): 1.6e9,
    ("metal", "kernel"): 8e9,
}
DIAGNOSTICS_FACTOR = 3.0
REFERENCE_STEPS = 10  # benchmarks/advection/solver.py: numpy steps per comparison
FIXED_OVERHEAD = 5.0  # process start, bandwidth probe, kernel compile (cached later)
COMPARABLE = 16.0  # past grids within this factor of the planned one set the rate
# Arrays alive at once: numpy-style code keeps ~14 temporaries of the grid around;
# the kernels two ping-pong buffers plus the diagnostics' few. The bandwidth probe
# adds three 256 MB arrays.
ARRAYS_ALIVE = {"array": 14, "kernel": 5}
PROBE_BYTES = 3 * 256 * 1024 * 1024
MEMORY_SHARE = 0.6  # of the host's RAM a single job may plan to use


def step_count(p: AdvectionParams, n: int, dims: int) -> int:
    speed = max(p.velocity, p.velocity_y) if dims == 2 else p.velocity
    return max(1, math.ceil(p.t_final / (p.cfl / n / speed)))


def work(p: AdvectionParams, dims: int) -> tuple[float, float]:
    """(cell updates on the backend, cell updates of the numpy reference)."""
    if p.mode == "throughput":
        n = p.resolutions[-1]
        cells = n**dims * dims
        return cells * (p.steps * p.repeats + 2), cells * REFERENCE_STEPS * p.repeats
    total = sum(n**dims * dims * (step_count(p, n, dims) * p.repeats + 2) for n in p.resolutions)
    coarsest = min(p.resolutions)
    return total * DIAGNOSTICS_FACTOR, coarsest**dims * dims * REFERENCE_STEPS


def peak_bytes(p: AdvectionParams, dims: int) -> int:
    side = max(p.resolutions)
    cells = side * side if dims == 2 else side
    itemsize = 8 if p.precision == "float64" else 4
    on_device = ARRAYS_ALIVE[p.implementation] * cells * itemsize
    reference = ARRAYS_ALIVE["array"] * cells * itemsize if p.mode == "throughput" else 0
    return on_device + reference + PROBE_BYTES


def past_rates(db: Database, host_id: str) -> dict[tuple[Any, ...], list[tuple[int, float]]]:
    """(backend, implementation, mode, dims, precision) -> [(cells, rate)], the
    finest grid of each past job, from timings with no caveat."""
    rates: dict[tuple[Any, ...], list[tuple[int, float]]] = {}
    for row in db.query(
        "SELECT results FROM jobs WHERE host_id = ? AND state = 'succeeded' "
        "AND results IS NOT NULL ORDER BY finished_at DESC LIMIT 200",
        (host_id,),
    ):
        results = loads(row["results"]) or {}
        if (results.get("environment") or {}).get("timing_caveat"):
            continue  # throttled or shared: not this host's real speed
        params = results.get("params") or {}
        runs = [r for r in results.get("runs") or [] if (r.get("cell_updates_per_s") or 0) > 0]
        if not runs:
            continue
        finest = max(runs, key=lambda r: r.get("cells") or r.get("nx") or 0)
        key = (
            str(results.get("backend")),
            str(params.get("implementation", "array")),
            str(params.get("mode", "convergence")),
            int(params.get("dims", 1)),
            str(params.get("precision", "float64")),
        )
        cells = int(finest.get("cells") or finest.get("nx") or 0)
        rates.setdefault(key, []).append((cells, float(finest["cell_updates_per_s"])))
    return rates


def _rate(
    history: dict[tuple[Any, ...], list[tuple[int, float]]], key: tuple[Any, ...], cells: int
) -> float | None:
    near = [rate for past, rate in history.get(key, []) if past and
            1 / COMPARABLE <= past / cells <= COMPARABLE]  # fmt: skip
    return statistics.median(near) if near else None


def estimate(db: Database, spec: ExperimentSpec, host_id: str, backend: str) -> dict[str, Any]:
    history = past_rates(db, host_id)
    jobs: list[float] = []
    measured = 0
    for v in spec.variants:
        p = v.params
        side = max(p.resolutions)
        cells = side * side if spec.dims == 2 else side
        on_backend, reference = work(p, spec.dims)
        rate = _rate(history, (backend, p.implementation, p.mode, spec.dims, p.precision), cells)
        if rate is not None:
            measured += 1
            # Measured rates already include the diagnostics of convergence runs.
            on_backend /= DIAGNOSTICS_FACTOR if p.mode == "convergence" else 1.0
        else:
            rate = DEFAULT_RATES.get((backend, p.implementation), DEFAULT_RATES[("cpu", "array")])
        seconds = on_backend / rate + FIXED_OVERHEAD
        if not (backend == "cpu" and p.implementation == "array"):
            cpu = _rate(history, ("cpu", "array", "throughput", spec.dims, p.precision), cells)
            seconds += reference / (cpu or DEFAULT_RATES[("cpu", "array")])
        jobs.append(seconds)
    basis = (
        "measured on this host" if measured == len(spec.variants)
        else "partly measured on this host" if measured else "rough default rates"
    )  # fmt: skip
    return {
        "seconds": round(sum(jobs), 1),
        "slowest_job_seconds": round(max(jobs), 1),
        "basis": basis,
        "peak_bytes": max(peak_bytes(v.params, spec.dims) for v in spec.variants),
    }
