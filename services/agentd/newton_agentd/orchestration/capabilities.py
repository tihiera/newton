"""What a host can run, derived from its last hardware probe, and where an
experiment should run ("auto" placement)."""

from __future__ import annotations

from typing import Any

BACKEND_PREFERENCE = ("cuda", "metal", "cpu")
# Below this many cells an Apple GPU loses to numpy (launch overhead; measured on an
# M3 Max the crossover is ~16k-131k cells for these 1D schemes), so "auto" only
# picks metal for runs this big. An explicit backend=metal is always honoured.
METAL_MIN_CELLS = 65_536


def capabilities(hardware: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{backend: {"ok": bool, "device"?: str, "reason"?: str}} for cpu/cuda/metal."""
    if not hardware:
        unknown = {"ok": False, "reason": "host not checked yet"}
        return {"cpu": {"ok": True}, "cuda": dict(unknown), "metal": dict(unknown)}
    packages = hardware.get("packages") or {}
    caps: dict[str, dict[str, Any]] = {"cpu": {"ok": True, "device": hardware.get("arch")}}

    gpus = hardware.get("gpus") or []
    if gpus:
        caps["cuda"] = _cuda(gpus[0], packages.get("cupy"), hardware.get("gpu_support") or {})
    elif hardware.get("gpu_error"):
        caps["cuda"] = {"ok": False, "reason": hardware["gpu_error"]}
    else:
        caps["cuda"] = {"ok": False, "reason": "no NVIDIA GPU on this host"}

    apple = hardware.get("apple_gpu")
    if apple and packages.get("mlx"):
        caps["metal"] = {"ok": True, "device": apple.get("name")}
    elif apple:
        caps["metal"] = {
            "ok": False,
            "device": apple.get("name"),
            "reason": f"{apple.get('name') or 'Apple GPU'} found, but MLX isn't installed",
        }
    else:
        caps["metal"] = {"ok": False, "reason": "no Apple GPU on this host"}
    return caps


def _cuda(gpu: dict[str, Any], cupy: str | None, support: dict[str, Any]) -> dict[str, Any]:
    """cuda is usable only once the worker's smoke test passed for exactly the CuPy
    and driver installed now (gpu.json): "CuPy is installed" alone has been wrong
    before (NVRTC too old for the GPU, missing CUDA libraries, crashes)."""
    name = gpu.get("name") or "NVIDIA GPU"
    driver = gpu.get("driver_version")
    out: dict[str, Any] = {
        "device": name,
        "compute_cap": gpu.get("compute_cap"),
        "unified_memory": gpu.get("unified_memory"),
        "driver": driver,
        "cupy": cupy,
    }
    current = support.get("cupy") == cupy and support.get("driver") == driver
    if cupy and current and support.get("ok"):
        return {**out, "ok": True}
    if support and not support.get("ok") and (current or not cupy):
        if support.get("deferred"):
            reason = "GPU support waits until the host's running jobs finish"
        else:
            reason = str(support.get("error") or "GPU support failed its check")
        return {**out, "ok": False, "reason": reason}
    if not cupy:
        reason = f"{name} found, but CuPy isn't installed on the host (install GPU support)"
    else:
        reason = (
            f"CuPy {cupy} is installed but not verified with driver {driver} yet "
            "(install GPU support checks it)"
        )
    return {**out, "ok": False, "reason": reason}


class PlacementError(ValueError):
    pass


def place(
    hosts: list[dict[str, Any]],
    host_id: str,
    backend: str,
    uses_float64: bool,
    max_cells: int = 0,
    needs_gpu: bool = False,
) -> tuple[dict[str, Any], str, str]:
    """Pick (host, backend, why). `hosts` are host views with `capabilities`.

    "auto" prefers a connected CUDA host (the GB10: float64 on the GPU, 128 GB),
    then the local Apple GPU (float32 runs only), then the local CPU. A fixed
    choice is checked, and refused with the host's own reason."""

    def usable(h: dict[str, Any], b: str) -> bool:
        if b == "metal" and uses_float64:
            return False
        if b == "cpu" and needs_gpu:
            return False  # hand-written kernels exist for CUDA and Metal only
        return bool(h["capabilities"][b]["ok"])

    def why_not(h: dict[str, Any], b: str) -> str:
        if b == "metal" and uses_float64:
            return "Apple GPUs have no float64"
        if b == "cpu" and needs_gpu:
            return "the kernel implementation needs a GPU"
        return str(h["capabilities"][b].get("reason") or "unavailable")

    def worth_it(b: str) -> bool:  # only consulted when the backend is "auto"
        return b != "metal" or max_cells >= METAL_MIN_CELLS or needs_gpu

    if host_id != "auto":
        host = next((h for h in hosts if h["id"] == host_id), None)
        if host is None:
            raise PlacementError(f"unknown host {host_id}")
        if backend != "auto":
            if not usable(host, backend):
                raise PlacementError(
                    f"{host['name']} can't run {backend}: {why_not(host, backend)}"
                )
            return host, backend, f"{backend} on {host['name']} (requested)"
        for b in BACKEND_PREFERENCE:
            if usable(host, b) and worth_it(b):
                return host, b, f"best backend on {host['name']}"
        reasons = "; ".join(f"{b}: {why_not(host, b)}" for b in BACKEND_PREFERENCE)
        raise PlacementError(f"{host['name']} can't run this experiment ({reasons})")

    # Remote hosts count only while connected; the local machine always does. GPUs
    # are sought on remote hosts first (the GB10); plain CPU work stays on this Mac.
    remote = [h for h in hosts if h["kind"] != "local" and h["status"] == "online"]
    local = [h for h in hosts if h["kind"] == "local"]
    candidates = remote + local
    wanted = BACKEND_PREFERENCE if backend == "auto" else (backend,)
    for b in wanted:
        if backend == "auto" and not worth_it(b):
            continue
        for host in (local + remote) if b == "cpu" else candidates:
            if usable(host, b):
                return host, b, f"auto: {b} on {host['name']}"
    reasons = "; ".join(
        f"{h['name']}: {why_not(h, b)}" for h in candidates for b in wanted if b != "cpu"
    )
    raise PlacementError(f"no connected host can run {backend}: {reasons}")
