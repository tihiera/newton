"""Hardware probe: GPUs via nvidia-smi, CPU, memory, disk, python packages."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from .gpu import read_state

NVIDIA_SMI_FIELDS = "index,name,memory.total,memory.free,driver_version,utilization.gpu"


def probe_gpus() -> tuple[list[dict[str, Any]], Optional[str]]:
    """NVIDIA GPUs and, if nvidia-smi exists but fails (e.g. a driver/library
    mismatch after an update without reboot), the reason, so a host never just
    looks GPU-less without explanation."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return [], None
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={NVIDIA_SMI_FIELDS}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return [], f"nvidia-smi failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        return [], f"nvidia-smi failed: {tail[0]}"
    gpus = parse_nvidia_smi(proc.stdout)
    _add_compute_capability(exe, gpus)
    return gpus, None


def _add_compute_capability(exe: str, gpus: list[dict[str, Any]]) -> None:
    # Separate query: older drivers reject the field, which must not empty the list.
    try:
        out = subprocess.run(
            [exe, "--query-gpu=index,compute_cap", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return
    if out.returncode != 0:
        return
    caps = {}
    for line in out.stdout.strip().splitlines():
        index, _, cap = (c.strip() for c in line.partition(","))
        caps[_int(index)] = cap or None
    for gpu in gpus:
        gpu["compute_cap"] = caps.get(gpu["index"])


def parse_nvidia_smi(out: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 6:
            continue
        gpus.append(
            {
                "index": _int(cols[0]),
                "name": cols[1],
                "memory_total_mib": _int(cols[2]),
                "memory_free_mib": _int(cols[3]),
                "driver_version": cols[4],
                "utilization_pct": _int(cols[5]),
                # GB10 / Grace-Blackwell report "[N/A]": GPU memory is the system RAM.
                "unified_memory": _int(cols[2]) is None,
            }
        )
    return gpus


def probe_apple_gpu() -> Optional[dict[str, Any]]:
    """The Apple Silicon GPU (Metal), or None on any other machine."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None
    gpu: dict[str, Any] = {"vendor": "apple", "unified_memory": True}
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        for entry in json.loads(out).get("SPDisplaysDataType", []):
            if "apple" in str(entry.get("spdisplays_vendor", "")).lower():
                gpu["name"] = entry.get("sppci_model") or entry.get("_name")
                gpu["cores"] = _int(str(entry.get("sppci_cores", "")))
                gpu["metal"] = str(entry.get("spdisplays_mtlgpufamilysupport", "")).replace(
                    "spdisplays_", ""
                )
                break
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    gpu["memory_total"] = memory_bytes().get("total")
    return gpu


def _int(s: str) -> Optional[int]:
    try:
        return int(float(s))
    except ValueError:
        return None


def memory_bytes() -> dict[str, Optional[int]]:
    total: Optional[int] = None
    available: Optional[int] = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            value = _int(rest.strip().split(" ")[0])
            if value is None:
                continue
            if key == "MemTotal":
                total = value * 1024
            elif key == "MemAvailable":
                available = value * 1024
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            ).stdout
            total = _int(out.strip())
        except (subprocess.SubprocessError, OSError):
            pass
    return {"total": total, "available": available}


# import name → distribution names to look for (CuPy ships per-CUDA wheels).
PACKAGES = {
    "numpy": ("numpy",),
    "matplotlib": ("matplotlib",),
    "cupy": ("cupy-cuda13x", "cupy-cuda12x", "cupy"),
    "mlx": ("mlx",),
    "torch": ("torch",),
}


def package_versions() -> dict[str, Optional[str]]:
    """Installed distribution versions for this interpreter (no imports)."""
    out: dict[str, Optional[str]] = {}
    for name, dists in PACKAGES.items():
        out[name] = None
        for dist in dists:
            try:
                out[name] = metadata.version(dist)
                break
            except metadata.PackageNotFoundError:
                continue
    return out


def bootstrap_info(root: Path) -> Optional[dict[str, Any]]:
    path = root / "bootstrap.json"
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data


def probe(root: Path) -> dict[str, Any]:
    disk = shutil.disk_usage(str(root))
    gpus, gpu_error = probe_gpus()
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory": memory_bytes(),
        "disk_free_bytes": disk.free,
        "gpus": gpus,
        "gpu_error": gpu_error,
        "apple_gpu": probe_apple_gpu(),
        "python_executable": sys.executable,
        "packages": package_versions(),
        "bootstrap": bootstrap_info(root),
        # The last GPU support check (gpu.json): what agentd's cuda capability uses.
        "gpu_support": read_state(root),
    }
