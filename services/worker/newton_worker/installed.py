"""The models this host already has, for the "new model service" form to offer.

  newton       in this worker's own stores (<root>/models/ollama, <root>/models/mlx):
               a service starts with no download and no approval;
  ollama       in the host's own Ollama store (~/.ollama/models, $OLLAMA_MODELS): reused
               without a download when its files can be hard-linked into Newton's store
               (same disk, owned by this user), else Newton downloads its own copy;
  huggingface  an mlx-community model in the Hugging Face cache: Newton downloads its own
               copy (approved like any download).

An Ollama revision is the manifest's sha256 (what `ollama list` shows as ID); an MLX
revision is the snapshot's commit. Stdlib only, Python 3.9.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from .services import GIB, MLX_COMPLETE, MLX_MODEL_RE, OLLAMA_MODEL_RE

REGISTRY = "registry.ollama.ai"
MAX_MANIFEST = 1 << 20
MAX_ENTRIES = 300
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def memory_hint(size_bytes: int) -> float:
    """GB to declare for a model of this size: its weights plus room for the KV cache."""
    return float(max(1, math.ceil(size_bytes / GIB * 1.2 + 1)))


def system_ollama_stores() -> list[Path]:
    """The host's own Ollama stores this user can read (not Newton's)."""
    candidates = [os.environ.get("OLLAMA_MODELS") or "", str(Path.home() / ".ollama" / "models"),
                  "/usr/share/ollama/.ollama/models", "/var/lib/ollama/models"]  # fmt: skip
    out: list[Path] = []
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        try:
            if (p / "manifests").is_dir() and p.resolve() not in [q.resolve() for q in out]:
                out.append(p)
        except OSError:
            continue
    return out


def _manifest(path: Path) -> Optional[tuple[str, dict[str, Any]]]:
    """(sha256 of the manifest, its JSON), or None when it isn't one."""
    try:
        if path.stat().st_size > MAX_MANIFEST:
            return None
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("layers"), list):
        return None
    return hashlib.sha256(raw).hexdigest(), data


def _blobs(data: dict[str, Any]) -> list[tuple[str, int]]:
    """(blob file name, size) of the config and every layer."""
    out = []
    for item in [data.get("config") or {}] + list(data.get("layers") or []):
        digest = str(item.get("digest") or "") if isinstance(item, dict) else ""
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            out.append((digest.replace(":", "-"), int(item.get("size") or 0)))
    return out


def ollama_models(store: Path) -> list[dict[str, Any]]:
    """name:tag, digest and size of every model in an Ollama store."""
    base = store / "manifests" / REGISTRY
    out: list[dict[str, Any]] = []
    try:
        namespaces = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return out
    for ns in namespaces:
        try:
            repos = sorted(p for p in ns.iterdir() if p.is_dir())
        except OSError:
            continue
        for repo in repos:
            try:
                tags = sorted(p for p in repo.iterdir() if p.is_file())
            except OSError:
                continue
            for tag in tags:
                found = _manifest(tag)
                if found is None:
                    continue
                name = repo.name if ns.name == "library" else f"{ns.name}/{repo.name}"
                model = f"{name}:{tag.name}"
                if not OLLAMA_MODEL_RE.match(model):
                    continue
                digest, data = found
                size = sum(s for _, s in _blobs(data))
                out.append({"model": model, "revision": digest, "size_bytes": size,
                            "manifest": tag, "data": data})  # fmt: skip
    return out


def _manifest_path(store: Path, model: str) -> Path:
    repo, tag = model.rsplit(":", 1) if ":" in model.rsplit("/", 1)[-1] else (model, "latest")
    parts = repo.split("/") if "/" in repo else ["library", repo]
    return store / "manifests" / REGISTRY / Path(*parts) / tag


def _linkable(src: Path, dst_dir: Path) -> bool:
    """Can `src` be hard-linked into `dst_dir`? Same disk, and owned by this user (Linux
    refuses links to other users' files under fs.protected_hardlinks)."""
    try:
        st = src.stat()
        target = dst_dir if dst_dir.exists() else dst_dir.parent
        while not target.exists():
            target = target.parent
        same_disk = st.st_dev == target.stat().st_dev
        mine = not hasattr(os, "getuid") or st.st_uid == os.getuid()
        return same_disk and mine and os.access(src, os.R_OK)
    except OSError:
        return False


def find_reusable(model: str, revision: str, root: Path) -> Optional[tuple[Path, dict[str, Any]]]:
    """A system Ollama store holding `model` at the pinned revision, whose files can be
    hard-linked into Newton's store (no download, no extra disk): (store, manifest)."""
    newton = root / "models" / "ollama"
    for store in system_ollama_stores():
        path = _manifest_path(store, model)
        found = _manifest(path) if path.is_file() else None
        if found is None or not found[0].startswith(revision):
            continue
        blobs = [store / "blobs" / name for name, _ in _blobs(found[1])]
        if blobs and all(b.is_file() and _linkable(b, newton / "blobs") for b in blobs):
            return store, found[1]
    return None


def reuse_ollama(model: str, revision: str, root: Path) -> bool:
    """Hard-link a model from the host's Ollama store into Newton's (True when done).
    The manifest goes last, so a half-linked model is never listed."""
    found = find_reusable(model, revision, root)
    if found is None:
        return False
    store, data = found
    newton = root / "models" / "ollama"
    (newton / "blobs").mkdir(parents=True, exist_ok=True)
    for name, _ in _blobs(data):
        dst = newton / "blobs" / name
        if not dst.exists():
            os.link(store / "blobs" / name, dst)
    src_manifest = _manifest_path(store, model)
    dst_manifest = _manifest_path(newton, model)
    dst_manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_manifest.with_name(dst_manifest.name + ".tmp")
    shutil.copyfile(src_manifest, tmp)
    os.replace(tmp, dst_manifest)
    return True


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size  # follows the cache's symlinks to its blobs
        except OSError:
            continue
    return total


def mlx_models(cache: Path, newton: bool) -> list[dict[str, Any]]:
    """mlx-community snapshots in a Hugging Face cache layout (Newton's: complete ones)."""
    out: list[dict[str, Any]] = []
    try:
        repos = sorted(p for p in cache.glob("models--mlx-community--*") if p.is_dir())
    except OSError:
        return out
    for repo in repos:
        model = repo.name[len("models--") :].replace("--", "/", 1)
        if not MLX_MODEL_RE.match(model):
            continue
        try:
            snaps = sorted(p for p in (repo / "snapshots").iterdir() if p.is_dir())
        except OSError:
            continue
        for snap in snaps:
            if not COMMIT_RE.match(snap.name) or (newton and not (snap / MLX_COMPLETE).is_file()):
                continue
            out.append({"model": model, "revision": snap.name, "size_bytes": _dir_size(snap)})
    return out


def scan(root: Path) -> list[dict[str, Any]]:
    """Every model this host already has, Newton's own first (see the module doc)."""
    entries: list[dict[str, Any]] = []
    seen = set()

    def add(engine: str, item: dict[str, Any], where: str, ready: bool) -> None:
        key = (engine, item["model"], item["revision"])
        if key in seen or len(entries) >= MAX_ENTRIES:
            return
        seen.add(key)
        entries.append({"engine": engine, "model": item["model"], "revision": item["revision"],
                        "size_bytes": item["size_bytes"],
                        "memory_gb_hint": memory_hint(item["size_bytes"]),
                        "where": where, "ready": ready})  # fmt: skip

    for item in ollama_models(root / "models" / "ollama"):
        add("ollama", item, "newton", True)
    for item in mlx_models(root / "models" / "mlx", newton=True):
        add("mlx", item, "newton", True)
    for store in system_ollama_stores():
        for item in ollama_models(store):
            reusable = find_reusable(item["model"], item["revision"], root) is not None
            add("ollama", item, "ollama", reusable)
    hf = os.environ.get("HF_HUB_CACHE") or str(Path.home() / ".cache" / "huggingface" / "hub")
    for item in mlx_models(Path(hf), newton=False):
        add("mlx", item, "huggingface", False)
    return entries
