"""Power state at run time: on a Mac, battery power or Low Power Mode slows both
CPU and GPU, so a timing without it can't be compared with another."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

POWER_MODES = {"0": "automatic", "1": "low_power", "2": "high_power"}


def power_state() -> dict[str, Any] | None:
    if sys.platform != "darwin":
        return None
    state: dict[str, Any] = {}
    try:
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
        first = batt.stdout.splitlines()[0] if batt.stdout else ""
        if "'" in first:
            state["source"] = first.split("'")[1]  # "AC Power" / "Battery Power"
        settings = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5)
        for line in settings.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("powermode", "lowpowermode"):
                mode = "1" if parts[0] == "lowpowermode" and parts[1] == "1" else parts[1]
                state["mode"] = POWER_MODES.get(mode, mode)
    except (OSError, subprocess.SubprocessError, IndexError):
        return state or None
    return state or None


def timing_caveat(state: dict[str, Any] | None) -> str | None:
    if not state:
        return None
    if state.get("mode") == "low_power":
        return "Low Power Mode was on: runtimes are not comparable"
    if state.get("source") == "Battery Power":
        return "ran on battery: runtimes may be throttled"
    return None


def gpu_activity(samples: int = 5, interval: float = 0.1) -> dict[str, Any] | None:
    """How busy the NVIDIA GPU is right before a timed run (a few samples: the
    utilization counter is instantaneous). On a GPU busy with other work (e.g. the
    owner's own CUDA jobs) timings measure contention, not the code. Processes that
    only hold GPU memory while idle are counted but don't make it "busy"."""
    import os
    import shutil
    import time

    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    busy: list[int] = []
    try:
        for i in range(samples):
            out = subprocess.run(
                [exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            ).stdout.split()  # fmt: skip
            if out and out[0].isdigit():
                busy.append(int(out[0]))
            if i + 1 < samples:
                time.sleep(interval)
        apps = subprocess.run(
            [exe, "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    others = [p for p in apps if p.strip().isdigit() and int(p) != os.getpid()]
    return {"utilization_pct": max(busy) if busy else None, "other_processes": len(others)}


def gpu_caveat(activity: dict[str, Any] | None) -> str | None:
    if not activity:
        return None
    busy = activity.get("utilization_pct")
    if busy is not None and busy > 10:
        others = activity.get("other_processes") or 0
        return (
            f"the GPU was busy with other work ({busy}% utilization, {others} other "
            f"process{'es' if others != 1 else ''}) before the run: runtimes are not comparable"
        )
    return None
