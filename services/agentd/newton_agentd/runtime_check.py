"""What this agentd runtime can do, checked without the network or the developer tools.

Newton runs from a git clone (B7: scripts/setup.sh, scripts/run.sh; no packaged app
ships). The benchmarks and the worker are read from NEWTON_RESOURCES_DIR (the clone by
default); a missing one is reported as a sentence (GET /health "runtime", the readiness
Engine item) rather than a crash at the first job.

Local jobs and the local worker run on agentd's own interpreter (sys.executable), so
the local host's CPU and Metal capabilities are what this interpreter can import.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import platform
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

# Paths under the resources dir that local and remote jobs need.
REQUIRED_RESOURCES = (
    ("benchmarks/advection/ir.py", "the benchmark code"),
    ("services/worker/newton_worker/__init__.py", "the worker"),
    ("services/worker/bootstrap.sh", "the remote host installer"),
)
NUMPY_MISSING = "numpy is missing from Newton's runtime"
MLX_MISSING = "MLX is missing from Newton's runtime, so the Apple GPU can't be used"
METAL_MIN_MACOS = 14
# How to get a missing part back: the clone is the only install there is.
RESTORE = (
    "restore it from git (or clone Newton again), or point NEWTON_RESOURCES_DIR at "
    "your Newton clone, then run scripts/setup.sh"
)


def interpreter_flags() -> list[str]:
    """The flags a child Python needs to see what this one sees (fixed argv)."""
    flags = []
    if sys.flags.no_user_site:
        flags.append("-s")
    if sys.flags.dont_write_bytecode or sys.dont_write_bytecode:
        flags.append("-B")
    return flags


def check(settings: Settings) -> dict[str, Any]:
    """{"packaged", "resources_ok", "problems": [sentences]}: cheap (a few stats).
    numpy is the local host's CPU capability (numpy_available), not a problem here.
    "packaged" is always false (no packaged app ships); kept for /health's shape."""
    root = Path(settings.resources_dir)
    problems = []
    if not root.is_dir():
        problems.append(
            f"Newton's folder ({root}) is missing, so experiments can't run; {RESTORE}."
        )
    else:
        for rel, what in REQUIRED_RESOURCES:
            if not (root / rel).is_file():
                problems.append(f"{rel} ({what}) is missing from {root}; {RESTORE}.")
    return {"packaged": False, "resources_ok": not problems, "problems": problems}


@functools.lru_cache(maxsize=1)
def numpy_available() -> bool:
    """numpy imports in this interpreter (once: an import is not undone)."""
    try:
        importlib.import_module("numpy")
    except Exception:  # an ImportError, or a broken build failing in its own way
        return False
    return True


def _mlx_present() -> bool:
    try:
        return importlib.util.find_spec("mlx.core") is not None
    except (ImportError, ValueError):
        return False


def macos_version() -> tuple[int, ...] | None:
    """(major, minor, ...) on macOS, else None."""
    if sys.platform != "darwin":
        return None
    release = platform.mac_ver()[0]
    try:
        return tuple(int(p) for p in release.split(".") if p)
    except ValueError:
        return None


def metal_problem() -> str | None:
    """Why the Apple GPU can't run Newton's Metal jobs from this runtime, or None.
    No GPU work happens here: mlx is located, not imported."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return "no Apple GPU on this host"
    version = macos_version()
    if version is not None and version[0] < METAL_MIN_MACOS:
        shown = ".".join(str(p) for p in version)
        return f"MLX needs macOS {METAL_MIN_MACOS} or later; this Mac runs macOS {shown}"
    if not _mlx_present():
        return MLX_MISSING
    return None
