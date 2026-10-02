"""Markdown validation report with provenance and reproduce commands."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts import ExperimentSpec, ValidationReport
from ..storage.db import Row, loads

BADGE = {"green": "🟢 green", "yellow": "🟡 yellow", "red": "🔴 red", "unknown": "⚪ unknown"}


def _ts(t: float | None) -> str:
    return "—" if t is None else datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _num(x: Any, spec: str = ".3e") -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int, float)):
        return format(x, spec)
    return str(x)


def comparison_plot(results: dict[str, dict[str, Any]], out: Path) -> str | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    with_errors = {
        label: res for label, res in results.items()
        if len(res.get("runs") or []) > 1 and "l2_error" in (res.get("runs") or [{}])[0]
    }  # fmt: skip
    if not with_errors:
        return None  # throughput runs: one grid, no error to refine
    fig, ax = plt.subplots(figsize=(6, 4.5))
    for label, res in with_errors.items():
        runs = res.get("runs") or []
        if runs:
            ax.loglog([r["dx"] for r in runs], [r["l2_error"] for r in runs], "o-", label=label)
    ax.set_xlabel("dx")
    ax.set_ylabel("L2 error")
    ax.set_title("Grid refinement")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def _speed_section(
    spec: ExperimentSpec, report: ValidationReport, results: dict[str, dict[str, Any]]
) -> list[str]:
    """Speed per variant against numpy on the same machine and against the
    bandwidth the same device measured (the roofline's roof)."""
    rows = []
    for ve in report.variants:
        res = results.get(ve.label) or {}
        m, ref = ve.metrics, res.get("reference") or {}
        if m.get("time_per_step") is None:
            continue  # convergence runs: timed with diagnostics, not a speed measurement
        params = res.get("params") or {}
        speedup = m.get("speedup_vs_numpy")
        fraction = m.get("bandwidth_fraction")
        roof = (
            "fits in cache" if m.get("cache_resident")
            else f"{fraction * 100:.0f}%" if fraction else "—"
        )  # fmt: skip
        agrees = m.get("agrees_with_numpy")
        same = (
            "— (is numpy)" if agrees is None
            else f"{_num(ref.get('max_relative_difference'), '.1e')} "
            f"({'same' if agrees else 'DIFFERENT'})"
        )  # fmt: skip
        rows.append(
            f"| {ve.label} | {params.get('implementation', 'array')} | "
            f"{_num(m.get('time_per_step') and m['time_per_step'] * 1e3, '.3f')} | "
            f"{_num(m.get('cell_updates_per_s'), '.3e')} | {_num(m.get('achieved_gbps'), '.1f')} | "
            f"{_num(m.get('peak_gbps'), '.1f')} | "
            f"{roof} | "
            f"{_num(speedup, '.1f')}{'x' if speedup else ''} | "
            f"{same} |"
        )
    if not rows:
        return []
    return [
        "## Speed and roofline",
        "",
        "Achieved bandwidth counts the minimum traffic of a sweep (each value read and "
        "written once); the peak is the copy/triad bandwidth the same device measured in "
        "the same job. Speedup and agreement are against numpy at the same precision on "
        "the same machine.",
        "",
        "| Variant | Implementation | ms/step | cell updates/s | GB/s | peak GB/s | of peak "
        "| vs numpy | difference from numpy |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
    ]


def _assumptions_section(results: dict[str, dict[str, Any]]) -> list[str]:
    """Each scheme's claims (its SchemeIR, or the library's) against what was measured."""
    rows = []
    for label, res in results.items():
        for a in res.get("assumptions") or []:
            measured = a.get("measured")
            if a.get("claim") == "max_cfl" and isinstance(measured, dict):
                at, beyond = measured.get("at_claim") or {}, measured.get("beyond") or {}
                measured = (f"{'stable' if at.get('stable') else 'unstable'} at {at.get('cfl')}"
                            f"; {'stable' if beyond.get('stable') else 'unstable'} at "
                            f"{beyond.get('cfl')}")  # fmt: skip
            elif isinstance(measured, float):
                measured = _num(measured, ".3g")
            holds = {True: "✅ holds", False: "❌ does not hold", None: "— not tested"}[
                a.get("holds")
            ]
            rows.append(f"| {label} | {a.get('claim')} | {a.get('claimed')} | {measured} | "
                        f"{holds} |")  # fmt: skip
    if not rows:
        return []
    scheme = next((r.get("environment", {}).get("scheme_ir") for r in results.values()
                   if r.get("environment", {}).get("scheme_ir")), None)  # fmt: skip
    note = f"Schemes given as documents (SchemeIR {scheme[:12]}…) run as generated code. " if (
        scheme) else ""  # fmt: skip
    return [
        "## Assumption checks",
        "",
        note + "Each claim a scheme makes, against what this run measured.",
        "",
        "| Variant | Claim | Claimed | Measured | |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ]


def write_report(
    report: ValidationReport,
    experiment: Row,
    jobs: list[Row],
    host: Row | None,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = ExperimentSpec.model_validate(loads(experiment["spec"]))
    jobs_by_label = {j["label"]: j for j in jobs}
    results = {j["label"]: loads(j["results"]) or {} for j in jobs}

    # Copy per-variant plots next to the report so relative links work.
    plot_links: dict[str, list[str]] = {}
    for label, j in jobs_by_label.items():
        adir = Path(j["artifacts_dir"]) if j["artifacts_dir"] else None
        for rel in (results.get(label) or {}).get("plots", []):
            src = adir / rel if adir else None
            if src and src.is_file():
                dst = out_dir / "plots" / f"{label}-{Path(rel).name}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                plot_links.setdefault(label, []).append(f"plots/{dst.name}")
    comparison = comparison_plot(results, out_dir / "plots" / "comparison.png")

    hw = loads(host["hardware"]) if host and host["hardware"] else {}
    accelerators = [g.get("name", "?") for g in hw.get("gpus") or []]
    if hw.get("apple_gpu"):
        apple = hw["apple_gpu"]
        accelerators.append(f"{apple.get('name', 'Apple GPU')} ({apple.get('cores', '?')}-core)")
    gpus = ", ".join(accelerators) or "no GPU"
    lines = [
        f"# {report.title}",
        "",
        f"**Evidence:** {BADGE[report.evidence]}  ",
        f"**Benchmark:** `{report.benchmark}` · **Backend:** `{spec.backend}` · "
        f"**Host:** {host['name'] if host else '?'} ({gpus})",
        "",
        report.summary,
        "",
    ]
    if spec.hypothesis:
        lines += ["## Hypothesis", "", spec.hypothesis, ""]

    lines += ["## Verdicts", ""]
    for verdict in report.verdicts:
        lines += [
            f"### {verdict.label} — {BADGE[verdict.evidence]}",
            "",
            "| Check | Result | Detail |",
            "|---|---|---|",
        ]
        for c in verdict.checks:
            mark = {True: "pass", False: "fail", None: "n/a"}[c.passed]
            lines.append(f"| {c.name} | {mark} | {c.detail} |")
        lines.append("")

    performance = spec.objective == "performance"
    keys = (
        ["time_per_step", "cell_updates_per_s", "conservation", "max_overshoot", "stable"]
        if performance
        else ["l2_error", "linf_error", "observed_order", "conservation", "max_overshoot",
              "tv_increase", "runtime", "stable"]
    )  # fmt: skip
    lines += [
        "## Metrics",
        "",
        "| Variant | Role | Job | Ran on | " + " | ".join(keys) + " |",
        "|" + "---|" * (len(keys) + 4),
    ]
    for ve in report.variants:
        cells = [_num(ve.metrics.get(k), ".2f" if k == "observed_order" else ".3e") for k in keys]
        ran = f"{ve.backend or '?'}: {ve.device or '?'}"
        lines.append(
            f"| {ve.label} | {ve.role} | {ve.job_state} | {ran} | " + " | ".join(cells) + " |"
        )
    lines.append("")

    lines += _speed_section(spec, report, results)

    if comparison:
        lines += ["## Plots", "", f"![grid refinement](plots/{comparison})", ""]
    elif plot_links:
        lines += ["## Plots", ""]
    for label, links in plot_links.items():
        lines += [f"**{label}:** " + " ".join(f"![{Path(p).stem}]({p})" for p in links), ""]

    refinement = [res for res in results.values() if "l2_error" in ((res.get("runs") or [{}])[0])]
    if refinement:
        lines += ["## Per-resolution results", ""]
    for label, res in results.items():
        runs = res.get("runs") or []
        if not runs:
            continue
        if "l2_error" not in runs[0]:
            continue  # throughput: shown in "Speed and roofline"
        lines += [
            f"**{label}** (`{res.get('scheme')}`)",
            "",
            "| nx | steps | L2 | L∞ | mass drift | overshoot | runtime (s) |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in runs:
            lines.append(
                f"| {r['nx']} | {r['steps']} | {_num(r['l2_error'])} | {_num(r['linf_error'])} | "
                f"{_num(r['mass_error'])} | {_num(r['max_overshoot'])} | "
                f"{_num(r['runtime_s'], '.4f')} |"
            )
        lines.append("")

    lines += [
        "## Reproduce",
        "",
        "From the repository root, for each variant write its params and run:",
        "",
    ]
    for v in spec.variants:
        lines += [
            f"**{v.label}**",
            "",
            "```bash",
            f"cat > params-{v.label}.json <<'EOF'",
            json.dumps({**v.params.model_dump(), "dims": spec.dims}, indent=2),
            "EOF",
            f"python -m benchmarks.advection --params params-{v.label}.json "
            f"--backend {spec.backend} --out out-{v.label}",
            "```",
            "",
        ]

    lines += _assumptions_section(results)
    prov = report.provenance
    lines += [
        "## Provenance",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Experiment | `{experiment['id']}` |",
        f"| Repository commit | `{prov.get('repository_commit') or 'unknown'}` |",
        f"| Host | {host['name'] if host else '?'} (`{experiment['host_id']}`) |",
        f"| Worker | {prov.get('worker_version') or '—'} |",
        f"| Created | {_ts(experiment['created_at'])} |",
        f"| Report generated | {_ts(prov.get('generated_at'))} |",
    ]
    for j in jobs:
        env = (results.get(j["label"]) or {}).get("environment", {})
        libs = ", ".join(f"{k} {env[k]}" for k in ("numpy", "cupy", "mlx") if env.get(k))
        power = env.get("power") or {}
        power_note = f", {power.get('source', '?')} / {power.get('mode', '?')}" if power else ""
        caveat = f" ⚠ {env['timing_caveat']}" if env.get("timing_caveat") else ""
        lease = ((prov.get("jobs") or {}).get(j["label"]) or {}).get("device_lease")
        if lease:  # what was paused, exactly; direct endpoint traffic isn't seen
            aborted = int(lease.get("aborted") or 0)
            finished = max(0, int(lease.get("in_flight_at_start") or 0) - aborted)
            loading = lease.get("loading_at_start") or []
            paused = (
                f"model requests routed through Newton paused on this host ({finished} let "
                f"finish, {aborted} aborted"
                + (f", waited for {', '.join(loading)} to load" if loading else "")
                + "; direct endpoint use not verified)"
            )
            caveat = f"{caveat} — {paused}" if caveat else f" · {paused}"
        lines.append(
            f"| Job {j['label']} | `{j['id']}` attempt {j['attempt']}, {j['state']}, "
            f"device {env.get('device', '—')}, {libs or 'numpy —'}{power_note}, "
            f"{_ts(j['started_at'])} → {_ts(j['finished_at'])}{caveat} |"
        )
    kernels = {
        name: digest
        for res in results.values()
        for name, digest in ((res.get("environment") or {}).get("kernels") or {}).items()
    }
    for name, digest in sorted(kernels.items()):
        lines.append(f"| Kernel `{name}` | sha256 `{digest[:16]}…` |")
    lines.append("")

    path = out_dir / "report.md"
    path.write_text("\n".join(lines))
    (out_dir / "report.json").write_text(report.model_dump_json(indent=2))
    return path
