"""Finite-volume schemes for u_t + a u_x = 0 (a > 0) on a periodic grid.

All schemes are written in conservative flux form, u_i -= c (F_{i+1/2} - F_{i-1/2}),
with c = a dt / dx and F the numerical flux divided by a. `axis` selects the
direction of a sweep on a 2D grid (dimension splitting); 1D arrays use axis 0.

kernels/advect.h implements the same formulas, operation for operation, for the
CUDA and Metal kernels: change both together.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Array = Any  # numpy.ndarray or cupy.ndarray
Limiter = Callable[[Any, Array], Array]


def minmod(xp: Any, r: Array) -> Array:
    return xp.maximum(0.0, xp.minimum(1.0, r))


def van_leer(xp: Any, r: Array) -> Array:
    ar = xp.abs(r)
    return (r + ar) / (1.0 + ar)


Flux = Callable[[Any, Array, float, int], Array]


def upwind_flux(xp: Any, u: Array, c: float, axis: int = 0) -> Array:
    """First-order upwind: F_{i+1/2} = u_i."""
    return u


def lax_wendroff_flux(xp: Any, u: Array, c: float, axis: int = 0) -> Array:
    """Second-order Lax–Wendroff: F_{i+1/2} = u_i + (1 - c)/2 (u_{i+1} - u_i)."""
    return u + 0.5 * (1.0 - c) * (xp.roll(u, -1, axis=axis) - u)


def limited_flux(limiter: Limiter) -> Flux:
    """Sweby flux-limited Lax–Wendroff (TVD for c <= 1 with a TVD limiter)."""

    def flux(xp: Any, u: Array, c: float, axis: int = 0) -> Array:
        du_right = xp.roll(u, -1, axis=axis) - u  # u_{i+1} - u_i
        du_left = u - xp.roll(u, 1, axis=axis)  # u_i - u_{i-1}
        nonzero = du_right != 0
        safe = xp.where(nonzero, du_right, 1.0)
        r = xp.where(nonzero, du_left / safe, 0.0)
        return u + 0.5 * (1.0 - c) * limiter(xp, r) * du_right

    return flux


SCHEMES: dict[str, Flux] = {
    "upwind": upwind_flux,
    "lax_wendroff": lax_wendroff_flux,
    "muscl_minmod": limited_flux(minmod),
    "muscl_vanleer": limited_flux(van_leer),
}

# Formal order of accuracy for smooth solutions (limiters clip at extrema).
FORMAL_ORDER = {"upwind": 1, "lax_wendroff": 2, "muscl_minmod": 2, "muscl_vanleer": 2}
# The scheme ids kernels/advect.h switches on.
SCHEME_IDS = {"upwind": 0, "lax_wendroff": 1, "muscl_minmod": 2, "muscl_vanleer": 3}


def step(xp: Any, u: Array, c: float, flux: Flux, axis: int = 0) -> Array:
    f = flux(xp, u, c, axis)
    return u - c * (f - xp.roll(f, 1, axis=axis))
