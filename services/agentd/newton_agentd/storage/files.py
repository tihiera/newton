"""Safe handling of files that come back from workers."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path, PurePosixPath


class UnsafeArchive(ValueError):
    pass


def safe_relative(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    parts = [x for x in p.parts if x != "."]
    if p.is_absolute() or not parts or ".." in parts or "\\" in name:
        raise UnsafeArchive(f"unsafe path {name!r}")
    return PurePosixPath(*parts)


def extract_tar_gz(data: bytes, dest: Path, max_bytes: int = 2 * 1024**3) -> list[str]:
    """Extract regular files and directories only; returns extracted file paths."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    files: list[str] = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            rel = safe_relative(m.name)
            target = (root / rel).resolve()
            if target != root and root not in target.parents:
                raise UnsafeArchive(f"path escapes destination: {m.name!r}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                raise UnsafeArchive(f"non-regular file in archive: {m.name!r}")
            total += m.size
            if total > max_bytes:
                raise UnsafeArchive("archive too large")
            src = tar.extractfile(m)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read())
            files.append(rel.as_posix())
    return sorted(files)


def resolve_inside(root: Path, rel: str) -> Path:
    """Resolve rel under root, refusing traversal and symlink escapes."""
    target = (root / safe_relative(rel)).resolve()
    base = root.resolve()
    if base not in target.parents:
        raise UnsafeArchive(f"path escapes root: {rel!r}")
    return target
