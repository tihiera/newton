"""Evidence rules."""

from __future__ import annotations

from typing import Any

from newton_agentd.research.evaluation import judge

BASE = {
    "l2_error": 1e-2,
    "observed_order": 1.0,
    "runtime": 1.0,
    "conservation": 0.0,
    "max_overshoot": 0.0,
    "stable": True,
}


def cand(**kw: Any) -> dict[str, Any]:
    return {**BASE, "l2_error": 1e-4, "observed_order": 2.0, "runtime": 2.0, **kw}


def test_green_when_everything_passes() -> None:
    evidence, checks = judge(BASE, cand(), "succeeded", "float64")
    assert evidence == "green", checks


def test_yellow_when_oscillatory_or_slow() -> None:
    assert judge(BASE, cand(max_overshoot=0.2), "succeeded", "float64")[0] == "yellow"
    assert judge(BASE, cand(runtime=50.0), "succeeded", "float64")[0] == "yellow"
    assert judge(BASE, cand(l2_error=8e-3), "succeeded", "float64")[0] == "yellow"


def test_red_cases() -> None:
    assert judge(BASE, cand(), "failed", "float64")[0] == "red"
    assert judge(BASE, cand(stable=False), "succeeded", "float64")[0] == "red"
    assert judge(BASE, cand(conservation=1e-3), "succeeded", "float64")[0] == "red"
    assert judge(BASE, cand(l2_error=2e-2), "succeeded", "float64")[0] == "red"


def test_float32_has_looser_conservation_tolerance() -> None:
    assert judge(BASE, cand(conservation=1e-6), "succeeded", "float32")[0] == "green"
