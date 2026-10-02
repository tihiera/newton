"""E2 review round: the rules that the first tests let slip (each from a surviving
mutation): operators, limiters, RK stepping, validation reasons, claim thresholds."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pytest

from benchmarks.advection import ir, solver
from benchmarks.advection.backends import ArrayBackend
from benchmarks.advection.schemes import step

SEMI = {"name": "semi", "flux": {"limiter": "none", "correction": 0.5},
        "claims": {"order": 2, "max_cfl": 0.5, "tvd": False}}  # fmt: skip


def rk(a: Any, b: Any) -> dict[str, Any]:
    return {**SEMI, "time": {"method": "rk", "tableau": {"a": a, "b": b}}}


# -- A(c): every operator, operand order ---------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ({"op": "add", "args": [0.25, "c"]}, lambda c: 0.25 + c),
        ({"op": "sub", "args": ["c", 0.25]}, lambda c: c - 0.25),
        ({"op": "sub", "args": [0.25, "c"]}, lambda c: 0.25 - c),
        ({"op": "mul", "args": [3.0, "c"]}, lambda c: 3.0 * c),
        ({"op": "div", "args": ["c", 4.0]}, lambda c: c / 4.0),
        ({"op": "div", "args": [1.0, {"op": "add", "args": [1.0, "c"]}]}, lambda c: 1 / (1 + c)),
        ({"op": "mul", "args": [{"op": "sub", "args": [1.0, "c"]},
                                {"op": "add", "args": ["c", 2.0]}]},
         lambda c: (1 - c) * (c + 2)),
    ],
)  # fmt: skip
def test_every_operator_evaluates_as_written(expr: Any, expected: Any) -> None:
    from benchmarks.advection.kernels import coefficients

    doc = ir.validate({**SEMI, "flux": {"limiter": "none", "correction": expr},
                       "claims": {"order": 1, "max_cfl": 0.9, "tvd": False}})  # fmt: skip
    for c in (0.1, 0.37, 0.8):
        assert ir.coefficient(doc, c) == expected(c)
        assert coefficients(c, doc) == (c, expected(c))


# -- limiters against their textbook formulas -------------------------------------------------

R = np.array([-2.0, -0.5, 0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 10.0])
TEXTBOOK = {
    "minmod": lambda r: np.maximum(0, np.minimum(1, r)),
    "van_leer": lambda r: (r + np.abs(r)) / (1 + np.abs(r)),
    "superbee": lambda r: np.maximum(0, np.maximum(np.minimum(2 * r, 1), np.minimum(r, 2))),
    "mc": lambda r: np.maximum(0, np.minimum(np.minimum(2 * r, (1 + r) / 2), 2)),
    "koren": lambda r: np.maximum(0, np.minimum(np.minimum(2 * r, (1 + 2 * r) / 3), 2)),
}


@pytest.mark.parametrize("name", sorted(TEXTBOOK))
def test_limiters_are_the_textbook_ones(name: str) -> None:
    np.testing.assert_allclose(ir.limiter_fn(name)(np, R), TEXTBOOK[name](R), rtol=0, atol=1e-15)
    # Sweby's TVD region: 0 <= phi <= min(2r, 2) for r > 0, phi = 0 for r <= 0.
    phi = ir.limiter_fn(name)(np, R)
    assert np.all(phi[R <= 0] == 0) and np.all(phi[R > 0] <= np.minimum(2 * R[R > 0], 2) + 1e-15)


@pytest.mark.parametrize("name", ["superbee", "mc", "koren"])
def test_the_cpp_limiters_say_the_same_thing(name: str) -> None:
    """The C++ snippet, transcribed line by line into Python, matches limiter_fn."""
    snippet = ir._PHI[name]
    assert "phi" in snippet and "T(0)" in snippet  # clamped below at 0
    for r in R:
        env: dict[str, float] = {"r": float(r)}
        for stmt in snippet.replace("T(", "float(").split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            stmt = stmt.removeprefix("float ").removeprefix("T ")
            target, expr = (x.strip() for x in stmt.split("=", 1))
            expr = _ternary(expr)
            env[target] = eval(expr, {"float": float}, env)  # noqa: S307 - our own snippet
        assert env["phi"] == pytest.approx(float(TEXTBOOK[name](np.array([r]))[0]), abs=1e-15)


def _ternary(expr: str) -> str:
    """C's (a < b) ? x : y as Python's (x if a < b else y)."""
    if "?" not in expr:
        return expr
    cond, rest = expr.split("?", 1)
    yes, no = rest.split(":", 1)
    return f"(({yes.strip()}) if {cond.strip()} else ({no.strip()}))"


# -- Runge-Kutta: any explicit tableau, stage by stage ---------------------------------------

RK3 = ([[0, 0, 0], [1, 0, 0], [0.25, 0.25, 0]], [1 / 6, 1 / 6, 2 / 3])
RK4 = ([[0, 0, 0, 0], [0.5, 0, 0, 0], [0, 0.5, 0, 0], [0, 0, 1, 0]], [1 / 6, 1 / 3, 1 / 3, 1 / 6])
MIDPOINT: tuple[Any, Any] = ([[0, 0], [0.5, 0]], [0, 1])


@pytest.mark.parametrize("tableau", [RK3, RK4, MIDPOINT])
def test_rk_stepping_matches_a_hand_written_stage_loop(tableau: Any) -> None:
    a, b = tableau
    doc = ir.validate(rk(a, b))
    stepper = solver.Stepper(ArrayBackend(), doc, "float64", "array")
    u = np.random.default_rng(5).standard_normal(64)
    c = 0.3
    flux = ir.numpy_flux(doc)
    ks: list[np.ndarray] = []
    for i in range(len(b)):
        v = u + sum(a[i][j] * ks[j] for j in range(i))
        ks.append(step(np, v, c, flux) - v)
    expected = u + sum(w * k for w, k in zip(b, ks))
    np.testing.assert_allclose(stepper.sweep(u, c, 0), expected, rtol=0, atol=1e-14)


@pytest.mark.parametrize(
    ("time", "message"),
    [
        ({"a": [[0, 1], [1, 0]], "b": [0.5, 0.5]}, "explicit"),
        ({"a": [[1, 0], [1, 0]], "b": [0.5, 0.5]}, "explicit"),  # nonzero diagonal
        ({"a": [[0, 0], [1, 0]], "b": [0.5, 0.6]}, "sum to 1"),
        ({"a": [[0] * 5] * 5, "b": [0.2] * 5}, "1 to 4 stages"),
        ({"a": [[0, 0], [float("inf"), 0]], "b": [0.5, 0.5]}, "finite"),
        ({"a": [[0, 0]], "b": [0.5, 0.5]}, "s x s"),
    ],
)
def test_rk_tableaus_are_refused_for_the_right_reason(time: Any, message: str) -> None:
    doc = {**SEMI, "time": {"method": "rk", "tableau": time}}
    with pytest.raises(ir.IRError, match=message):
        ir.validate(doc)


def test_rk_with_a_c_dependent_correction_is_refused() -> None:
    doc = {**rk(*MIDPOINT), "flux": {"limiter": "none", "correction": ir.LW}}
    with pytest.raises(ir.IRError, match="semi-discrete"):
        ir.validate(doc)


def test_a_one_step_document_drops_any_tableau() -> None:
    doc = ir.validate({**SEMI, "time": {"method": "one_step", "tableau": {"a": [[0]], "b": [1]}}})
    assert doc["time"] == {"method": "one_step"}


def test_header_hashes_follow_the_formula() -> None:
    a = copy.deepcopy(ir.LIBRARY["muscl_koren"])
    b = copy.deepcopy(a)
    b["flux"]["correction"] = {"op": "mul", "args": [0.4, {"op": "sub", "args": [1.0, "c"]}]}
    assert ir.header_hash(a) == ir.header_hash(a)
    assert ir.digest(a) != ir.digest(b)  # another A(c): another document


# -- claim thresholds ----------------------------------------------------------------------------


def test_the_stability_check_has_a_sharp_edge() -> None:
    lw = ir.validate(ir.LIBRARY["lax_wendroff"])
    assert solver.stability_probe(lw, 1.0)["stable"] is True
    assert solver.stability_probe(lw, 1.02)["stable"] is False
    vl = ir.validate(ir.LIBRARY["muscl_vanleer"])  # limited: probed, transits scale with c
    probe = solver.stability_probe(vl, 0.1)
    assert probe["stable"] is True and "probe" in probe["method"]
    assert int(probe["method"].split(",")[1].split()[0]) >= 4 * solver.STABILITY_CELLS / 0.1


def test_named_schemes_claim_what_the_library_says() -> None:
    for name in ("upwind", "lax_wendroff", "muscl_minmod", "muscl_vanleer"):
        claims = solver.claims_of({"scheme": name})
        assert claims == ir.validate(ir.LIBRARY[name])["claims"]


def test_nondeterminism_is_caught() -> None:
    params = {"scheme": "lax_wendroff", "resolutions": [64], "initial_condition": "sine",
              "t_final": 1.0, "velocity": 1.0, "cfl": 0.5}  # fmt: skip
    stepper = solver.Stepper(ArrayBackend(), "lax_wendroff", "float64", "array")
    assert solver.deterministic(stepper, params, "float64", 1) is True
    calls = {"n": 0}
    real = stepper.advance

    def noisy(u: Any, n: int, courant: Any) -> Any:
        calls["n"] += 1
        out = real(u, n, courant)
        return out + (1e-15 if calls["n"] > solver.REFERENCE_STEPS else 0.0)

    stepper.advance = noisy  # type: ignore[method-assign]
    assert solver.deterministic(stepper, params, "float64", 1) is False
