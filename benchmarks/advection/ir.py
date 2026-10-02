"""Engine E2: numerical methods as data.

A scheme for u_t + a u_x = 0 is a `SchemeIR` document, not code:

    {
      "name": "lax_wendroff",
      "flux": {
        "limiter": "none",                     # φ(r): a fixed list, see LIMITERS
        "correction": {"op": "mul", "args": [0.5, {"op": "sub", "args": [1.0, "c"]}]}
      },                                       # A(c); null: first-order upwind
      "time": {"method": "one_step"},          # or "rk" with an explicit tableau
      "claims": {"order": 2, "max_cfl": 1.0, "tvd": false}
    }

Every scheme is in conservative flux form, u_i -= c (F_{i+1/2} - F_{i-1/2}), with

    F_{i+1/2} = u_i + A(c) φ(r_i) (u_{i+1} - u_i),   r_i = (u_i - u_{i-1}) / (u_{i+1} - u_i)

so conservation holds by construction, and the claims (order, largest stable CFL,
TVD) are what the experiment checks.

Trusted generators turn a document into code: numpy array operations, and a C++
header for the CUDA and Metal sweep kernels (advect.cu / advect.metal, unchanged).
They only assemble fixed snippets chosen by enum values: no text from a document,
model or paper ever becomes code. A(c) is a tiny typed expression tree evaluated in
double precision on the host, in the order it is written, which is what makes the
IR form of a scheme match its hand-written kernel bit for bit.

This module imports nothing beyond the standard library at load time, so agentd can
validate documents and hash generated code without numpy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any

MAX_DEPTH = 6
OPS = ("add", "sub", "mul", "div")
LIMITERS = ("none", "minmod", "van_leer", "superbee", "mc", "koren")
# A limiter is TVD (for 0 <= c <= 1 with the Lax–Wendroff correction) when its graph
# stays in Sweby's region: 0 <= φ(r) <= min(2r, 2) for r > 0, φ = 0 for r <= 0.
TVD_LIMITERS = ("minmod", "van_leer", "superbee", "mc", "koren")
MAX_STAGES = 4


class IRError(ValueError):
    pass


# -- validation --------------------------------------------------------------------------


def validate(doc: Any) -> dict[str, Any]:
    """The canonical form of a SchemeIR document, or IRError."""
    if not isinstance(doc, dict):
        raise IRError("a scheme is an object")
    unknown = set(doc) - {"name", "description", "flux", "time", "claims", "source"}
    if unknown:
        raise IRError(f"unknown fields: {sorted(unknown)}")
    name = doc.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise IRError("name: 1-64 ASCII letters, digits, '_' or '-'")
    flux = doc.get("flux")
    if not isinstance(flux, dict) or set(flux) - {"limiter", "correction"}:
        raise IRError("flux: {limiter, correction}")
    limiter = flux.get("limiter", "none")
    if limiter not in LIMITERS:
        raise IRError(f"limiter must be one of {list(LIMITERS)}")
    correction = flux.get("correction")
    if correction is None and limiter != "none":
        raise IRError("a limiter needs a correction A(c) to limit")
    if correction is not None:
        _check_expr(correction, 0)
        correction = _canon(correction)
    time = doc.get("time") or {"method": "one_step"}
    if not isinstance(time, dict) or time.get("method") not in ("one_step", "rk"):
        raise IRError("time.method is one_step or rk")
    if time["method"] == "rk":
        time = {"method": "rk", "tableau": _check_tableau(time.get("tableau"))}
        if correction is not None and _uses_c(correction):
            raise IRError("with rk the flux is semi-discrete: A may not depend on c")
    else:
        time = {"method": "one_step"}
    claims = doc.get("claims")
    if not isinstance(claims, dict) or set(claims) - {"order", "max_cfl", "tvd"}:
        raise IRError("claims: {order, max_cfl, tvd}")
    order, max_cfl, tvd = claims.get("order"), claims.get("max_cfl"), claims.get("tvd", False)
    if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 4:
        raise IRError("claims.order is an integer in [1, 4]")
    _number(max_cfl, "claims.max_cfl")
    assert isinstance(max_cfl, (int, float))  # (checked just above)
    if not 0 < float(max_cfl) <= 2:
        raise IRError("claims.max_cfl is in (0, 2]")
    if correction is not None:  # A(c) must be a number wherever it will be evaluated
        for i in range(1, 41):
            c = i / 40 * max(1.0, 1.25 * float(max_cfl))
            try:
                a_of_c = _eval(correction, c)
            except (ArithmeticError, IRError):
                raise IRError(f"A(c) can't be evaluated at c = {c:.3g}") from None
            if not math.isfinite(a_of_c):
                raise IRError(f"A(c) is not finite at c = {c:.3g}")
    if not isinstance(tvd, bool):
        raise IRError("claims.tvd is true or false")
    out = {
        "name": name,
        "flux": {"limiter": limiter, "correction": correction},
        "time": time,
        "claims": {"order": order, "max_cfl": float(max_cfl), "tvd": tvd},
    }
    for optional in ("description", "source"):
        value = doc.get(optional)
        if value is not None:
            if not isinstance(value, str) or len(value) > 2000:
                raise IRError(f"{optional} is text (at most 2000 characters)")
            out[optional] = value
    return out


def _check_expr(e: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise IRError("A(c) is nested too deeply")
    if e == "c":
        return
    if isinstance(e, bool):
        raise IRError("A(c): numbers, 'c' and {op, args}")
    if isinstance(e, (int, float)):
        _number(e, "A(c)")
        return
    if not isinstance(e, dict) or set(e) != {"op", "args"} or e["op"] not in OPS:
        raise IRError(f"A(c): numbers, 'c' and {{op, args}} with op in {list(OPS)}")
    if not isinstance(e["args"], list) or len(e["args"]) != 2:
        raise IRError("A(c): every op takes exactly two args")
    for arg in e["args"]:
        _check_expr(arg, depth + 1)


def _uses_c(e: Any) -> bool:
    if e == "c":
        return True
    return isinstance(e, dict) and any(_uses_c(a) for a in e["args"])


def _check_tableau(t: Any) -> dict[str, Any]:
    if not isinstance(t, dict) or set(t) != {"a", "b"}:
        raise IRError("rk tableau: {a, b} (explicit: a is strictly lower triangular)")
    a, b = t["a"], t["b"]
    if not isinstance(b, list) or not 1 <= len(b) <= MAX_STAGES:
        raise IRError(f"rk tableau: 1 to {MAX_STAGES} stages")
    s = len(b)
    if (
        not isinstance(a, list)
        or len(a) != s
        or any(not isinstance(row, list) or len(row) != s for row in a)
    ):
        raise IRError("rk tableau: a is s x s")
    for i, row in enumerate(a):
        for j, x in enumerate(row):
            _number(x, "rk tableau")
            if j >= i and float(x) != 0.0:
                raise IRError("rk tableau: explicit methods only (a[i][j] = 0 for j >= i)")
    for x in b:
        _number(x, "rk tableau")
    if abs(sum(float(x) for x in b) - 1.0) > 1e-12:
        raise IRError("rk tableau: the weights b sum to 1 (consistency)")
    return {"a": [[float(x) for x in row] for row in a], "b": [float(x) for x in b]}


def _number(x: Any, where: str) -> None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise IRError(f"{where}: finite numbers only")
    try:
        finite = math.isfinite(float(x))
    except (OverflowError, ValueError):
        finite = False
    if not finite or abs(float(x)) > 1e12:
        raise IRError(f"{where}: finite numbers only (|x| <= 1e12)")


def _canon(e: Any) -> Any:
    """Numbers as floats (1 and 1.0: one document, one digest), -0.0 as 0.0."""
    if e == "c":
        return e
    if isinstance(e, dict):
        return {"op": e["op"], "args": [_canon(a) for a in e["args"]]}
    return float(e) + 0.0


# -- evaluation -------------------------------------------------------------------------


def coefficient(ir: dict[str, Any], c: float) -> float | None:
    """A(c) in double precision, evaluated exactly as written (None: upwind)."""
    e = ir["flux"]["correction"]
    return None if e is None else _eval(e, float(c))


def _eval(e: Any, c: float) -> float:
    if e == "c":
        return c
    if not isinstance(e, dict):
        return float(e)
    x, y = (_eval(a, c) for a in e["args"])
    if e["op"] == "add":
        return x + y
    if e["op"] == "sub":
        return x - y
    if e["op"] == "mul":
        return x * y
    if y == 0.0:
        raise IRError("A(c) divides by zero")
    return x / y


def von_neumann(ir: dict[str, Any], c: float, samples: int = 721) -> float:
    """max over wavenumbers of |amplification| for an unlimited (linear) scheme at
    Courant number c: exact, not a simulation (limited schemes are nonlinear)."""
    import cmath

    doc = validate(ir)
    k = coefficient(doc, c)
    a = 0.0 if k is None else k
    tableau = doc["time"].get("tableau")
    worst = 0.0
    for j in range(samples):
        theta = 2 * math.pi * j / (samples - 1)
        em, ep = cmath.exp(-1j * theta), cmath.exp(1j * theta)  # u_{i-1}, u_{i+1} over u_i
        dflux = (1 + a * (ep - 1)) - (em + a * (1 - em))  # F_{i+1/2} - F_{i-1/2}
        if tableau is None:
            g = 1 - c * dflux
        else:  # explicit RK on du/dt = -dflux (per unit c), stage by stage
            z = -c * dflux
            stages: list[complex] = []
            for i, row in enumerate(tableau["a"]):
                stages.append(z * (1 + sum(row[jj] * stages[jj] for jj in range(i))))
            g = 1 + sum(b * s for b, s in zip(tableau["b"], stages))
        worst = max(worst, abs(g))
    return worst


def digest(ir: dict[str, Any]) -> str:
    """sha256 of the canonical document: what an approval and a result refer to."""
    return hashlib.sha256(json.dumps(validate(ir), sort_keys=True).encode()).hexdigest()


def method_digest(ir: dict[str, Any]) -> str:
    """sha256 of what the scheme computes (flux and time stepping): the same method
    under another name, or with other claims, is the same method (memory, dedup)."""
    doc = validate(ir)
    method = {"flux": doc["flux"], "time": doc["time"]}
    return hashlib.sha256(json.dumps(method, sort_keys=True).encode()).hexdigest()


# -- generator: numpy -------------------------------------------------------------------


def limiter_fn(name: str) -> Callable[[Any, Any], Any]:
    """φ(r) as array operations, in the operation order of schemes.py."""

    def minmod(xp: Any, r: Any) -> Any:
        return xp.maximum(0.0, xp.minimum(1.0, r))

    def van_leer(xp: Any, r: Any) -> Any:
        ar = xp.abs(r)
        return (r + ar) / (1.0 + ar)

    def superbee(xp: Any, r: Any) -> Any:
        return xp.maximum(0.0, xp.maximum(xp.minimum(2.0 * r, 1.0), xp.minimum(r, 2.0)))

    def mc(xp: Any, r: Any) -> Any:
        return xp.maximum(0.0, xp.minimum(xp.minimum(2.0 * r, 0.5 * (1.0 + r)), 2.0))

    def koren(xp: Any, r: Any) -> Any:
        return xp.maximum(0.0, xp.minimum(xp.minimum(2.0 * r, (1.0 + 2.0 * r) / 3.0), 2.0))

    return {"minmod": minmod, "van_leer": van_leer, "superbee": superbee, "mc": mc,
            "koren": koren}[name]  # fmt: skip


def numpy_flux(ir: dict[str, Any]) -> Callable[[Any, Any, float, int], Any]:
    """F_{i+1/2} as array operations (same signature as the fluxes in schemes.py)."""
    limiter = ir["flux"]["limiter"]
    phi = None if limiter == "none" else limiter_fn(limiter)

    def flux(xp: Any, u: Any, c: float, axis: int = 0) -> Any:
        k = coefficient(ir, c)
        if k is None:
            return u
        du_right = xp.roll(u, -1, axis=axis) - u  # u_{i+1} - u_i
        if phi is None:
            return u + k * du_right
        du_left = u - xp.roll(u, 1, axis=axis)  # u_i - u_{i-1}
        nonzero = du_right != 0
        safe = xp.where(nonzero, du_right, 1.0)
        r = xp.where(nonzero, du_left / safe, 0.0)
        return u + k * phi(xp, r) * du_right

    return flux


# -- generator: C++ for the CUDA and Metal kernels ---------------------------------------

_PHI = {
    "minmod": (
        "    phi = (r < T(1)) ? r : T(1);\n"
        "    phi = (phi > T(0)) ? phi : T(0);\n"
    ),
    "van_leer": (
        "    T ar = (r < T(0)) ? -r : r;\n"
        "    phi = (r + ar) / (T(1) + ar);\n"
    ),
    "superbee": (
        "    T a = T(2) * r; a = (a < T(1)) ? a : T(1);\n"
        "    T b = (r < T(2)) ? r : T(2);\n"
        "    phi = (a > b) ? a : b; phi = (phi > T(0)) ? phi : T(0);\n"
    ),
    "mc": (
        "    T a = T(2) * r; T b = T(0.5) * (T(1) + r);\n"
        "    phi = (a < b) ? a : b; phi = (phi < T(2)) ? phi : T(2);\n"
        "    phi = (phi > T(0)) ? phi : T(0);\n"
    ),
    "koren": (
        "    T a = T(2) * r; T b = (T(1) + T(2) * r) / T(3);\n"
        "    phi = (a < b) ? a : b; phi = (phi < T(2)) ? phi : T(2);\n"
        "    phi = (phi > T(0)) ? phi : T(0);\n"
    ),
}  # fmt: skip


def cpp_header(ir: dict[str, Any]) -> str:
    """newton_flux / newton_update for advect.cu and advect.metal (which include it in
    place of advect.h). k = A(c) arrives from the host, computed by coefficient()."""
    limiter = ir["flux"]["limiter"]
    if ir["flux"]["correction"] is None:
        body = "    return u0;\n"
    elif limiter == "none":
        body = "    T dr = up - u0;  // u_{i+1} - u_i\n    return u0 + k * dr;\n"
    else:
        body = (
            "    T dr = up - u0;  // u_{i+1} - u_i\n"
            "    T dl = u0 - um;  // u_i - u_{i-1}\n"
            "    T r = (dr != T(0)) ? dl / dr : T(0);\n"
            "    T phi;\n" + _PHI[limiter] + "    return u0 + k * phi * dr;\n"
        )
    return (
        f"// Generated by Newton from SchemeIR {ir['name']} ({digest(ir)[:16]}).\n"
        "// Do not edit: change the document and regenerate.\n"
        "template <typename T, int SCHEME>\n"
        "NEWTON_FN T newton_flux(T um, T u0, T up, T k) {\n" + body + "}\n\n"
        "template <typename T, int SCHEME>\n"
        "NEWTON_FN T newton_update(T umm, T um, T u0, T up, T c, T k) {\n"
        "    T right = newton_flux<T, SCHEME>(um, u0, up, k);\n"
        "    T left = newton_flux<T, SCHEME>(umm, um, u0, k);\n"
        "    return u0 - c * (right - left);\n"
        "}\n"
    )


def header_hash(ir: dict[str, Any]) -> str:
    return hashlib.sha256(cpp_header(ir).encode()).hexdigest()


# -- the library: the hand-written schemes, as documents ---------------------------------

LW = {"op": "mul", "args": [0.5, {"op": "sub", "args": [1.0, "c"]}]}  # 0.5 * (1 - c)

LIBRARY: dict[str, dict[str, Any]] = {
    "upwind": {"name": "upwind", "flux": {"limiter": "none", "correction": None},
               "claims": {"order": 1, "max_cfl": 1.0, "tvd": True}},
    "lax_wendroff": {"name": "lax_wendroff", "flux": {"limiter": "none", "correction": LW},
                     "claims": {"order": 2, "max_cfl": 1.0, "tvd": False}},
    "muscl_minmod": {"name": "muscl_minmod", "flux": {"limiter": "minmod", "correction": LW},
                     "claims": {"order": 2, "max_cfl": 1.0, "tvd": True}},
    "muscl_vanleer": {"name": "muscl_vanleer",
                      "flux": {"limiter": "van_leer", "correction": LW},
                      "claims": {"order": 2, "max_cfl": 1.0, "tvd": True}},
    "muscl_superbee": {"name": "muscl_superbee",
                       "flux": {"limiter": "superbee", "correction": LW},
                       "claims": {"order": 2, "max_cfl": 1.0, "tvd": True}},
    "muscl_mc": {"name": "muscl_mc", "flux": {"limiter": "mc", "correction": LW},
                 "claims": {"order": 2, "max_cfl": 1.0, "tvd": True}},
    "muscl_koren": {"name": "muscl_koren", "flux": {"limiter": "koren", "correction": LW},
                    "claims": {"order": 2, "max_cfl": 1.0, "tvd": True}},
    # Semi-discrete MUSCL (van Leer) with SSP-RK2 (Heun): TVD for c <= 1/2 (the
    # limiter's own bound times the SSP coefficient), so that is what it claims.
    "muscl_vanleer_ssprk2": {
        "name": "muscl_vanleer_ssprk2",
        "flux": {"limiter": "van_leer", "correction": 0.5},
        "time": {"method": "rk", "tableau": {"a": [[0, 0], [1, 0]], "b": [0.5, 0.5]}},
        "claims": {"order": 2, "max_cfl": 0.5, "tvd": True},
    },
}  # fmt: skip
