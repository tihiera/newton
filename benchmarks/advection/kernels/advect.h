// Finite-volume advection: one dimension-split sweep, shared by the CUDA
// (advect.cu) and Metal (advect.metal) kernels. The includer defines NEWTON_FN.
//
// These are the formulas of schemes.py, operation for operation and in the same
// order, so that float64 results match numpy's to round-off. Change both together.
// k = 0.5 * (1 - c) is computed by the caller in double precision, as numpy does.
//
// SCHEME: 0 upwind, 1 Lax-Wendroff, 2 MUSCL minmod, 3 MUSCL van Leer.

// F_{i+1/2} / a from u_{i-1}, u_i, u_{i+1}.
template <typename T, int SCHEME>
NEWTON_FN T newton_flux(T um, T u0, T up, T k) {
    if (SCHEME == 0) {
        return u0;
    }
    T dr = up - u0;  // u_{i+1} - u_i
    if (SCHEME == 1) {
        return u0 + k * dr;
    }
    T dl = u0 - um;  // u_i - u_{i-1}
    T r = (dr != T(0)) ? dl / dr : T(0);
    T phi;
    if (SCHEME == 2) {  // minmod: max(0, min(1, r))
        phi = (r < T(1)) ? r : T(1);
        phi = (phi > T(0)) ? phi : T(0);
    } else {  // van Leer: (r + |r|) / (1 + |r|)
        T ar = (r < T(0)) ? -r : r;
        phi = (r + ar) / (T(1) + ar);
    }
    return u0 + k * phi * dr;
}

// u_i at the next time level, from u_{i-2} .. u_{i+1}.
template <typename T, int SCHEME>
NEWTON_FN T newton_update(T umm, T um, T u0, T up, T c, T k) {
    T right = newton_flux<T, SCHEME>(um, u0, up, k);
    T left = newton_flux<T, SCHEME>(umm, um, u0, k);
    return u0 - c * (right - left);
}
