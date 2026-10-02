"""Job bundles: deterministic tar.gz of benchmark code + params.json."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache", ".mypy_cache"}
# Python, data, and the hand-written GPU kernels (CUDA .cu, Metal .metal, shared .h).
BUNDLED_SUFFIXES = (".py", ".json", ".txt", ".md", ".cu", ".metal", ".h")
KERNEL_SUFFIXES = (".cu", ".metal", ".h")


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def build_bundle(benchmarks_dir: Path | None, files: dict[str, Any] | None = None) -> bytes:
    """Bundle `benchmarks/` (as the `benchmarks` package) plus extra JSON files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if benchmarks_dir is not None:
            for path in sorted(benchmarks_dir.rglob("*")):
                rel = path.relative_to(benchmarks_dir)
                if any(part in EXCLUDE_DIRS for part in rel.parts) or path.is_symlink():
                    continue
                if path.is_file() and path.suffix in BUNDLED_SUFFIXES:
                    _add_bytes(tar, f"benchmarks/{rel.as_posix()}", path.read_bytes())
        for name, content in (files or {}).items():
            data = json.dumps(content, indent=2, sort_keys=True).encode()
            _add_bytes(tar, name, data)
    return buf.getvalue()


def kernel_hashes(benchmarks_dir: Path) -> dict[str, str]:
    """sha256 of every kernel source that goes into a bundle, by bundle path."""
    out = {}
    for path in sorted(benchmarks_dir.rglob("*")):
        rel = path.relative_to(benchmarks_dir)
        if (
            path.is_file()
            and path.suffix in KERNEL_SUFFIXES
            and not (set(rel.parts) & EXCLUDE_DIRS)
        ):
            out[f"benchmarks/{rel.as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def repository_commit(root: Path) -> str | None:
    """HEAD commit of the resources repo, suffixed with +dirty if modified."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head.returncode != 0:
            return None
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "benchmarks"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None
