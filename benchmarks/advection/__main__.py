"""`python -m benchmarks.advection --params params.json [--out DIR] [--backend cpu|cuda|metal]`

Writes results.json and plots/ into --out (default: current directory).
The backend defaults to $NEWTON_BACKEND (set by the worker) or cpu.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

from .backends import BackendUnavailable, canonical, names
from .ir import IRError
from .ir import validate as validate_ir
from .schemes import SCHEMES
from .solver import claims_of, run_study


def benchmark_name(params: dict[str, Any]) -> str:
    return f"linear_advection_{int(params.get('dims', 1))}d"


def validate(params: dict[str, Any]) -> dict[str, Any]:
    p = {
        "dims": 1,
        "mode": "convergence",
        "implementation": "array",
        "resolutions": [64, 128, 256, 512, 1024],
        "cfl": 0.5,
        "velocity": 1.0,
        "velocity_y": 0.5,
        "t_final": 1.0,
        "initial_condition": "sine",
        "precision": "float64",
        "repeats": 3,
        "steps": 100,
        **params,
    }
    if p.get("scheme") == "ir":
        try:
            p["scheme_ir"] = validate_ir(p.get("scheme_ir"))
        except IRError as e:
            raise SystemExit(f"invalid scheme_ir: {e}") from None
    elif p.get("scheme") not in SCHEMES:
        raise SystemExit(f"unknown scheme {p.get('scheme')!r}; choose from {sorted(SCHEMES)}")
    if not 0 < float(p["cfl"]) <= 1:
        raise SystemExit("cfl must be in (0, 1]")
    if p["initial_condition"] not in ("sine", "gaussian", "square"):
        raise SystemExit("unknown initial_condition")
    if p["dims"] not in (1, 2):
        raise SystemExit("dims must be 1 or 2")
    if p["mode"] not in ("convergence", "throughput"):
        raise SystemExit("mode must be convergence or throughput")
    if p["implementation"] not in ("array", "kernel"):
        raise SystemExit("implementation must be array or kernel")
    if not 1 <= int(p["steps"]) <= 1_000_000:
        raise SystemExit("steps must be in [1, 1000000]")
    return p


def plot(runs: list[dict[str, Any]], scheme: str, out: Path, mode: str) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    import numpy as np

    plots = out / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    written = []
    finest = runs[-1]
    if finest.get("dims", 1) == 2:
        fig, axes = plt.subplots(1, 2 if mode == "convergence" else 1, figsize=(9, 4),
                                 squeeze=False)  # fmt: skip
        panels = [(finest["_solution"], f"{scheme}, {finest['nx']}x{finest['nx']}")]
        if mode == "convergence":
            panels.append((finest["_exact"], "exact"))
        for ax, (field, title) in zip(axes[0], panels):
            image = ax.imshow(field, origin="lower", extent=(0, 1, 0, 1), cmap="RdBu_r")
            ax.set_title(title)
            fig.colorbar(image, ax=ax, shrink=0.8)
        fig.tight_layout()
        fig.savefig(plots / "solution.png", dpi=110)
        plt.close(fig)
        written.append("plots/solution.png")
    if mode != "convergence":
        return written

    fig, ax = plt.subplots(figsize=(5, 4))
    dx = [r["dx"] for r in runs]
    ax.loglog(dx, [r["l2_error"] for r in runs], "o-", label=scheme)
    ref = runs[0]["l2_error"]
    for order in (1, 2):
        ax.loglog(
            dx, [ref * (d / dx[0]) ** order for d in dx], "--", alpha=0.4, label=f"O(dx^{order})"
        )
    ax.set_xlabel("dx")
    ax.set_ylabel("L2 error")
    ax.set_title(f"Convergence: {scheme}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(plots / "convergence.png", dpi=120)
    plt.close(fig)
    written.append("plots/convergence.png")

    if finest.get("dims", 1) == 2:
        return written
    x = (np.arange(finest["nx"]) + 0.5) * finest["dx"]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.plot(x, finest["_exact"], "k-", lw=1, label="exact")
    ax.plot(x, finest["_solution"], "-", lw=1.2, label=scheme)
    ax.set_title(f"Solution at t_final, nx={finest['nx']}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(plots / "solution.png", dpi=120)
    plt.close(fig)
    written.append("plots/solution.png")
    return written


def fail(
    out: Path, message: str, backend: str | None = None, params: dict[str, Any] | None = None
) -> NoReturn:
    """A clear, machine-readable failure instead of a traceback."""
    print(f"[advection] error: {message}", file=sys.stderr, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps(
            {"benchmark": benchmark_name(params or {}), "backend": backend, "error": message}
        )
    )
    sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(prog="benchmarks.advection")
    parser.add_argument("--params", required=True, type=Path)
    parser.add_argument("--out", default=".", type=Path)
    parser.add_argument("--backend", choices=[*names(), "gpu"])
    args = parser.parse_args()

    params = validate(json.loads(args.params.read_text()))
    try:
        backend = canonical(args.backend or os.environ.get("NEWTON_BACKEND", "cpu"))
    except ValueError as e:
        fail(args.out, str(e), params=params)
    shown = f"{params['scheme_ir']['name']} (IR)" if params["scheme"] == "ir" else params["scheme"]
    print(
        f"[advection] {benchmark_name(params)} {params['mode']} scheme={shown} "
        f"backend={backend} implementation={params['implementation']} "
        f"resolutions={params['resolutions']}",
        flush=True,
    )
    started = time.time()
    try:
        study = run_study(params, backend)
    except (BackendUnavailable, ValueError, ArithmeticError) as e:
        fail(args.out, str(e), backend, params)

    roof = study["bandwidth"]
    if roof.get("peak_gbps"):
        print(
            f"[advection] measured bandwidth: copy {roof['copy_gbps']:.1f} GB/s, "
            f"triad {roof['triad_gbps']:.1f} GB/s",
            flush=True,
        )
    else:
        print(f"[advection] bandwidth probe failed: {roof.get('error')}", flush=True)
    for r in study["runs"]:
        error = f"L2={r['l2_error']:.3e} " if "l2_error" in r else ""
        print(
            f"[advection] n={r['nx']:>6} cells={r['cells']:>10} steps={r['steps']:>7} {error}"
            f"mass={r['mass_error']:.1e} t={r['runtime_s']:.3f}s "
            f"({r['cell_updates_per_s']:.3e} cell updates/s)",
            flush=True,
        )
    if study["reference"]:
        ref = study["reference"]
        print(
            f"[advection] vs numpy ({ref['steps']} steps): max relative difference "
            f"{ref['max_relative_difference']:.2e} ({'agrees' if ref['agrees'] else 'DIFFERS'}); "
            f"numpy {ref['numpy_time_per_step_s'] * 1e3:.2f} ms/step",
            flush=True,
        )
    args.out.mkdir(parents=True, exist_ok=True)
    name = params["scheme_ir"]["name"] if params["scheme"] == "ir" else params["scheme"]
    plots = plot(study["runs"], name, args.out, params["mode"])
    results = {
        "benchmark": benchmark_name(params),
        "scheme": name,
        "formal_order": claims_of(params)["order"],
        "assumptions": study["assumptions"],
        "backend": backend,
        "params": params,
        "metrics": study["metrics"],
        "observed_order": study["observed_order"],
        "bandwidth": roof,
        "reference": study["reference"],
        "runs": [{k: v for k, v in r.items() if not k.startswith("_")} for r in study["runs"]],
        "environment": {
            **study["environment"],
            "python": platform.python_version(),
            "hostname": platform.node(),
        },
        "plots": plots,
        "started_at": started,
        "finished_at": time.time(),
        "reproduce": f"python -m benchmarks.advection --params {args.params} --backend {backend}",
    }
    (args.out / "results.json").write_text(json.dumps(results, indent=2))
    m = study["metrics"]
    if params["mode"] == "throughput":
        fraction = m["bandwidth_fraction"]
        roof_note = (
            "fits in cache: no roofline" if m.get("cache_resident")
            else f"{fraction:.0%} of measured peak" if fraction else "no roof measured"
        )  # fmt: skip
        speedup = m["speedup_vs_numpy"]
        print(
            f"[advection] done: {m['time_per_step'] * 1e3:.3f} ms/step, "
            f"{m['achieved_gbps']:.1f} GB/s ({roof_note})"
            + (f", {speedup:.1f}x numpy" if speedup else ""),
            flush=True,
        )
    else:
        order, l2 = m["observed_order"], m["l2_error"]
        limited = (study["observed_order"] or {}).get("roundoff_limited") or []
        print(
            "[advection] done: "
            + (f"L2={l2:.3e} on {m['l2_grid']} cells " if l2 is not None else "no usable grid ")
            + (f"order={order:.3f}" if order is not None else "order=n/a")
            + (f" (round-off limited: {limited})" if limited else ""),
            flush=True,
        )


if __name__ == "__main__":
    main()
