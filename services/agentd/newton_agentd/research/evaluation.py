"""Turn baseline-vs-candidate metrics into evidence (green/yellow/red/unknown).

Accuracy experiments (per candidate, relative to the baseline):
  red      candidate failed, went unstable, broke conservation, or has higher error
  green    ≥2× lower L2 error, observed order ≥ baseline + 0.5, no new extrema,
           and at most 5× the baseline runtime
  yellow   lower error but at least one of the green criteria is missed
  unknown  baseline missing/failed, or metrics absent

Performance experiments (same grid, steps and precision in every variant):
  red      candidate failed, went unstable, broke conservation, computes different
           numbers than numpy, or is slower than the baseline
  green    ≥1.2× the baseline's speed with all of the above intact
  yellow   about as fast (between 1/1.2× and 1.2×)

Always: a result that ran on another backend, computed different numbers than
numpy, or ran different kernel code than was approved never counts.
"""

from __future__ import annotations

from typing import Any

from ..contracts import (
    CandidateVerdict,
    Check,
    Evidence,
    ExperimentSpec,
    ValidationReport,
    VariantEvaluation,
)
from ..storage.db import Row, loads

ACCURACY_GAIN = 0.5  # candidate L2 must be ≤ 50% of baseline for green
ORDER_GAIN = 0.5
MAX_RUNTIME_RATIO = 5.0
SPEEDUP_GAIN = 1.2  # performance: candidate must reach 1.2x the baseline's speed
OVERSHOOT_TOL = 1e-3
CONSERVATION_TOL = {"float64": 1e-9, "float32": 1e-4}

EVIDENCE_RANK: dict[Evidence, int] = {"unknown": 0, "red": 1, "yellow": 2, "green": 3}


def _fmt(x: Any, spec: str = ".3e") -> str:
    return "n/a" if x is None else format(x, spec)


def judge(
    base: dict[str, Any], cand: dict[str, Any], cand_state: str, precision: str
) -> tuple[Evidence, list[Check]]:
    checks: list[Check] = []
    if cand_state != "succeeded" or not cand:
        checks.append(Check(name="completed", passed=False, detail=f"candidate job {cand_state}"))
        return "red", checks
    checks.append(Check(name="completed", passed=True, detail="candidate job succeeded"))

    stable = bool(cand.get("stable"))
    checks.append(
        Check(
            name="stable",
            passed=stable,
            detail="finite, bounded solution"
            if stable
            else "solution blew up or became non-finite",
        )
    )

    tol = CONSERVATION_TOL.get(precision, 1e-9)
    cons = cand.get("conservation")
    cons_ok = cons is not None and cons <= tol
    checks.append(
        Check(
            name="conservation",
            passed=cons_ok,
            detail=f"max mass drift {_fmt(cons)} (tolerance {tol:g})",
        )
    )

    b_l2, c_l2 = base.get("l2_error"), cand.get("l2_error")
    ratio = c_l2 / b_l2 if b_l2 and c_l2 is not None else None
    grids = (base.get("l2_grid"), cand.get("l2_grid"))
    if None not in grids and grids[0] != grids[1]:
        ratio = None  # errors from different grids (one hit the round-off cut-off sooner)
    improved = ratio is not None and ratio < 1.0
    checks.append(
        Check(
            name="accuracy",
            passed=None if ratio is None else ratio <= ACCURACY_GAIN,
            detail=f"L2 {_fmt(c_l2)} vs baseline {_fmt(b_l2)} (ratio {_fmt(ratio, '.3f')}; "
            f"green needs ≤ {ACCURACY_GAIN})"
            + (
                f"; not comparable: finest usable grids differ ({grids[0]} vs {grids[1]}, "
                "round-off)"
                if None not in grids and grids[0] != grids[1]
                else ""
            ),
        )
    )

    b_ord, c_ord = base.get("observed_order"), cand.get("observed_order")
    checks.append(
        Check(
            name="convergence_order",
            passed=None if b_ord is None or c_ord is None else c_ord >= b_ord + ORDER_GAIN,
            detail=f"observed order {_fmt(c_ord, '.2f')} vs baseline {_fmt(b_ord, '.2f')}",
        )
    )

    overshoot = cand.get("max_overshoot")
    checks.append(
        Check(
            name="non_oscillatory",
            passed=None if overshoot is None else overshoot <= OVERSHOOT_TOL,
            detail=f"max new extremum {_fmt(overshoot)}; "
            f"TV increase {_fmt(cand.get('tv_increase'))}",
        )
    )

    b_rt, c_rt = base.get("runtime"), cand.get("runtime")
    rt_ratio = c_rt / b_rt if b_rt and c_rt is not None else None
    checks.append(
        Check(
            name="cost",
            passed=None if rt_ratio is None else rt_ratio <= MAX_RUNTIME_RATIO,
            detail=f"runtime ratio {_fmt(rt_ratio, '.2f')}× (green needs ≤ {MAX_RUNTIME_RATIO:g}×)",
        )
    )

    if not stable or not cons_ok or (ratio is not None and not improved):
        return "red", checks
    if all(c.passed is True for c in checks):
        return "green", checks
    return "yellow", checks


def judge_performance(
    base: dict[str, Any], cand: dict[str, Any], cand_state: str, precision: str
) -> tuple[Evidence, list[Check]]:
    checks: list[Check] = []
    if cand_state != "succeeded" or not cand:
        checks.append(Check(name="completed", passed=False, detail=f"candidate job {cand_state}"))
        return "red", checks
    checks.append(Check(name="completed", passed=True, detail="candidate job succeeded"))
    stable = bool(cand.get("stable"))
    checks.append(Check(name="stable", passed=stable, detail="finite, bounded solution"
                        if stable else "solution blew up or became non-finite"))  # fmt: skip
    tol = CONSERVATION_TOL.get(precision, 1e-9)
    cons = cand.get("conservation")
    cons_ok = cons is not None and cons <= tol
    checks.append(
        Check(
            name="conservation",
            passed=cons_ok,
            detail=f"mean mass drift {_fmt(cons)} (tolerance {tol:g})",
        )  # fmt: skip
    )
    agrees = cand.get("agrees_with_numpy")
    checks.append(
        Check(
            name="same_result",
            passed=None if agrees is None else bool(agrees),
            detail=(
                "no comparison (numpy compared with itself)"
                if agrees is None
                else f"max relative difference from numpy {_fmt(cand.get('reference_agreement'))}"
            ),
        )
    )
    b_t, c_t = base.get("time_per_step"), cand.get("time_per_step")
    speedup = b_t / c_t if b_t and c_t else None
    gbps, frac = cand.get("achieved_gbps"), cand.get("bandwidth_fraction")
    roof = (
        "the grid fits in cache: no roofline" if cand.get("cache_resident")
        else f"{_fmt(frac and frac * 100, '.0f')}% of measured peak"
    )  # fmt: skip
    checks.append(
        Check(
            name="speed",
            passed=None if speedup is None else speedup >= SPEEDUP_GAIN,
            detail=f"{_fmt(speedup, '.2f')}x the baseline ({_fmt(c_t and c_t * 1e3, '.3f')} vs "
            f"{_fmt(b_t and b_t * 1e3, '.3f')} ms/step; green needs ≥ {SPEEDUP_GAIN}x); "
            f"{_fmt(gbps, '.1f')} GB/s ({roof})",
        )
    )
    if not stable or not cons_ok or agrees is False:
        return "red", checks
    if speedup is not None and speedup < 1 / SPEEDUP_GAIN:
        return "red", checks
    if all(c.passed is True for c in checks):
        return "green", checks
    return "yellow", checks


def integrity_problem(
    variant: VariantEvaluation, manifest: dict[str, Any], results: dict[str, Any]
) -> str | None:
    """Why a finished result can't be trusted at all, or None."""
    if variant.metrics.get("agrees_with_numpy") is False:
        return (
            "computed different numbers than numpy at the same precision "
            f"(max relative difference {_fmt(variant.metrics.get('reference_agreement'))})"
        )
    approved = {
        name.rsplit("/", 1)[-1]: digest
        for name, digest in (manifest.get("kernel_hashes") or {}).items()
    }
    env = results.get("environment") or {}
    ran = env.get("kernels") or {}
    if approved and ran and any(approved.get(name) != digest for name, digest in ran.items()):
        return "ran kernel code that differs from the code that was approved"
    approved_ir = manifest.get("scheme_ir_digest")
    if approved_ir and variant.job_state == "succeeded" and env.get("scheme_ir") != approved_ir:
        return "ran a scheme document that differs from the one that was approved"
    if any(a.get("claim") == "deterministic" and a.get("holds") is False
           for a in variant.assumptions):  # fmt: skip
        return "gave different numbers for the same steps run twice (not deterministic)"
    if approved and not ran and variant.job_state == "succeeded":
        return "did not report which kernel code it ran"
    return None


CLAIMS = ("order", "max_cfl", "tvd")


def check_claims(evidence: Evidence, checks: list[Check], cand: VariantEvaluation
                 ) -> tuple[Evidence, list[Check]]:  # fmt: skip
    """The scheme's own claims (from its SchemeIR, or the library's for a named one):
    one that the run contradicts is a finding, and the best a candidate can then be
    is yellow. Integrity claims (determinism, agreement) are handled as red above."""
    tested = [a for a in cand.assumptions if a.get("claim") in CLAIMS
              and a.get("holds") is not None]  # fmt: skip
    if not tested:
        return evidence, checks
    broken = [a for a in tested if a["holds"] is False]
    detail = "; ".join(
        f"{a['claim']}: claimed {a.get('claimed')}, measured {_claim_measure(a)}" for a in broken
    ) or "; ".join(f"{a['claim']} {a.get('claimed')} holds" for a in tested)
    checks = [*checks, Check(name="claims", passed=not broken, detail=detail)]
    if broken and evidence == "green":
        evidence = "yellow"
    return evidence, checks


def _claim_measure(a: dict[str, Any]) -> str:
    m = a.get("measured")
    if a.get("claim") == "max_cfl" and isinstance(m, dict):
        at = m.get("at_claim") or {}
        state = "stable" if at.get("stable") else "unstable"
        return f"{state} (growth {_fmt(at.get('growth'), '.2g')})"
    return _fmt(m, ".3g") if isinstance(m, (int, float)) else str(m)


def summarize(label: str, evidence: Evidence, checks: list[Check]) -> str:
    failed = [c.name for c in checks if c.passed is False]
    unknown = [c.name for c in checks if c.passed is None]
    if evidence == "green":
        return f"{label}: all checks passed."
    parts = []
    if failed:
        parts.append("failed " + ", ".join(failed))
    if unknown:
        parts.append("inconclusive " + ", ".join(unknown))
    return f"{label}: " + ("; ".join(parts) if parts else evidence)


def discount_timing(
    evidence: Evidence, checks: list[Check], caveat: str
) -> tuple[Evidence, list[Check]]:
    """Timings taken throttled (battery, Low Power Mode) or on a GPU shared with other
    work prove nothing either way."""
    out = [
        Check(name=c.name, passed=None, detail=f"{c.detail}; {caveat}")
        if c.name in ("cost", "speed")
        else c
        for c in checks
    ]
    if (
        evidence == "red"
        and any(c.name == "speed" and c.passed is False for c in checks)
        and all(c.passed is not False for c in out)
    ):
        evidence = "yellow"  # "slower" came only from the untrustworthy timing
    return ("yellow" if evidence == "green" else evidence), out


def backend_mismatch(expected: str, variant: VariantEvaluation) -> str | None:
    """A result only counts on the backend it was approved for."""
    if variant.backend is None:
        return None  # results from before backends were recorded
    if variant.backend != expected:
        return f"ran on {variant.backend}, but the experiment asked for {expected}"
    if expected in ("cuda", "metal") and (variant.device or "").startswith("cpu"):
        return f"asked for {expected} but ran on {variant.device}"
    return None


def evaluate(experiment: Row, jobs: list[Row], provenance: dict[str, Any]) -> ValidationReport:
    spec = ExperimentSpec.model_validate(loads(experiment["spec"]))
    by_label = {j["label"]: j for j in jobs}
    variants: list[VariantEvaluation] = []
    caveats: dict[str, str] = {}
    problems: dict[str, str] = {}
    for v in spec.variants:
        j = by_label.get(v.label)
        results = (loads(j["results"]) or {}) if j else {}
        manifest = (loads(j.get("manifest")) or {}) if j else {}
        ran = results.get("backend")
        caveat = (results.get("environment") or {}).get("timing_caveat")
        if caveat:
            caveats[v.label] = str(caveat)
        variants.append(
            VariantEvaluation(
                label=v.label,
                role=v.role,
                job_id=j["id"] if j else "",
                job_state=j["state"] if j else "missing",
                metrics=(loads(j["metrics"]) or {}) if j else {},
                backend="cuda" if ran == "gpu" else ran,
                device=(results.get("environment") or {}).get("device"),
                assumptions=results.get("assumptions") or [],
            )
        )
        problem = integrity_problem(variants[-1], manifest, results)
        if problem and variants[-1].job_state == "succeeded":
            problems[v.label] = problem
    base = next(v for v in variants if v.role == "baseline")
    precision = next(x.params.precision for x in spec.variants if x.role == "baseline")

    verdicts: list[CandidateVerdict] = []
    for cand in (v for v in variants if v.role == "candidate"):
        wrong = backend_mismatch(spec.backend, cand)
        if wrong and cand.job_state == "succeeded":
            checks = [Check(name="backend", passed=False, detail=wrong)]
            verdicts.append(
                CandidateVerdict(
                    label=cand.label,
                    evidence="red",
                    checks=checks,
                    summary=f"{cand.label}: {wrong}",
                )
            )
            continue
        if cand.label in problems:
            checks = [Check(name="integrity", passed=False, detail=problems[cand.label])]
            verdicts.append(
                CandidateVerdict(label=cand.label, evidence="red", checks=checks,
                                 summary=f"{cand.label}: {problems[cand.label]}")
            )  # fmt: skip
            continue
        base_wrong = backend_mismatch(spec.backend, base) or problems.get(base.label)
        if base_wrong and base.job_state == "succeeded":
            checks = [Check(name="baseline", passed=None, detail=base_wrong)]
            verdicts.append(
                CandidateVerdict(
                    label=cand.label,
                    evidence="unknown",
                    checks=checks,
                    summary=f"{cand.label}: baseline {base_wrong}",
                )
            )
            continue
        if base.job_state != "succeeded" or not base.metrics:
            checks = [Check(name="baseline", passed=None, detail=f"baseline job {base.job_state}")]
            verdicts.append(
                CandidateVerdict(
                    label=cand.label,
                    evidence="unknown",
                    checks=checks,
                    summary=f"{cand.label}: baseline unavailable.",
                )
            )
            continue
        judge_fn = judge_performance if spec.objective == "performance" else judge
        evidence, checks = judge_fn(base.metrics, cand.metrics, cand.job_state, precision)
        caveat = caveats.get(cand.label) or caveats.get(base.label)
        if caveat:
            evidence, checks = discount_timing(evidence, checks, caveat)
        evidence, checks = check_claims(evidence, checks, cand)
        verdicts.append(
            CandidateVerdict(
                label=cand.label,
                evidence=evidence,
                checks=checks,
                summary=summarize(cand.label, evidence, checks),
            )
        )

    best = max(verdicts, key=lambda v: EVIDENCE_RANK[v.evidence])
    summary = f"Best candidate: {best.label} ({best.evidence}). " + " ".join(
        v.summary for v in verdicts
    )
    return ValidationReport(
        experiment_id=experiment["id"],
        title=experiment["title"],
        benchmark=spec.benchmark,
        evidence=best.evidence,
        summary=summary,
        variants=variants,
        verdicts=verdicts,
        provenance=provenance,
    )
