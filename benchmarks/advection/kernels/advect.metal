// Body of the Metal kernel built by mx.fast.metal_kernel (MLX writes the
// signature). Same sweep as advect.cu: row-major (NY, NX) grid, AXIS 1 = x,
// 0 = y, one thread per cell. Template values: T, SCHEME, AXIS, NX, NY.
// Inputs: u, coef = [c, k]. Output: out.
uint idx = thread_position_in_grid.x;
if (idx >= uint(NX * NY)) {
    return;
}
int i = int(idx) % NX;
int j = int(idx) / NX;
T v[4];
for (int o = 0; o < 4; ++o) {
    int d = o - 2;
    int n = (AXIS == 1) ? j * NX + (i + d + NX) % NX : ((j + d + NY) % NY) * NX + i;
    v[o] = u[n];
}
out[idx] = newton_update<T, SCHEME>(v[0], v[1], v[2], v[3], coef[0], coef[1]);
