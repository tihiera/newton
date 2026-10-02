"""Filesystem helpers: atomic JSON writes and safe tar handling."""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any


class UnsafePathError(ValueError):
    pass


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data: dict[str, Any] = json.load(f)
    return data


def check_relative(name: str) -> PurePosixPath:
    """Validate a relative POSIX path: no absolute paths, no '..', no empty parts."""
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/") or "\\" in name:
        raise UnsafePathError(f"absolute or non-posix path not allowed: {name!r}")
    parts = [part for part in p.parts if part != "."]
    if not parts or any(part == ".." for part in parts):
        raise UnsafePathError(f"path escapes its root: {name!r}")
    return PurePosixPath(*parts)


def safe_extract_tar_gz(data: bytes, dest: Path, max_bytes: int = 512 * 1024 * 1024) -> int:
    """Extract a .tar.gz into dest, accepting only regular files and directories."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            rel = check_relative(member.name)
            target = (root / rel).resolve()
            if root != target and root not in target.parents:
                raise UnsafePathError(f"path escapes bundle root: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise UnsafePathError(f"only regular files allowed in bundle: {member.name!r}")
            total += member.size
            if total > max_bytes:
                raise UnsafePathError("bundle exceeds size limit")
            src = tar.extractfile(member)
            if src is None:
                raise UnsafePathError(f"unreadable member: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                out.write(src.read())
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            count += 1
    return count


def pack_paths_tar_gz(root: Path, rel_paths: Iterable[str]) -> bytes:
    """Pack the given relative paths (files or directories) under root.

    Missing paths are skipped. Symlinks are never followed or included.
    """
    root = root.resolve()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in rel_paths:
            rel = check_relative(name.rstrip("/"))
            start = root / rel
            if start.is_symlink() or not start.exists():
                continue
            if start.is_file():
                tar.add(str(start), arcname=str(rel), recursive=False)
                continue
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not Path(dirpath, d).is_symlink())
                for fname in sorted(filenames):
                    fpath = Path(dirpath, fname)
                    if fpath.is_symlink() or not fpath.is_file():
                        continue
                    tar.add(str(fpath), arcname=str(fpath.relative_to(root)), recursive=False)
    return buf.getvalue()
