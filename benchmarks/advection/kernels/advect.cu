// One sweep of 2D (or 1D: ny = 1) periodic advection along AXIS, on a row-major
// (ny, nx) grid. AXIS follows numpy: 1 = x (contiguous), 0 = y. One thread per cell.
// Compiled by CuPy's RawModule (NVRTC) with --fmad=false, so multiply-adds round
// like numpy's.

template <typename T, int SCHEME, int AXIS>
__global__ void advect_sweep(const T* __restrict__ u, T* __restrict__ out, const T c,
                             const T k, const int nx, const int ny) {
    const long long idx = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= (long long)nx * ny) {
        return;
    }
    const int i = (int)(idx % nx);
    const int j = (int)(idx / nx);
    T v[4];  // u at offsets -2, -1, 0, +1 along AXIS
    for (int o = 0; o < 4; ++o) {
        const int d = o - 2;
        const long long n = (AXIS == 1) ? (long long)j * nx + (i + d + nx) % nx
                                        : (long long)((j + d + ny) % ny) * nx + i;
        v[o] = u[n];
    }
    out[idx] = newton_update<T, SCHEME>(v[0], v[1], v[2], v[3], c, k);
}
