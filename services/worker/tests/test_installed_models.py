"""What a machine already has (newton_worker.installed): Newton's own stores, the host's
Ollama store (reused by hard link: no download, no approval) and the Hugging Face cache."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from newton_worker import installed
from newton_worker import services as svc


def ollama_model(store: Path, name: str, tag: str, layers: dict[str, bytes]) -> str:
    """An Ollama store entry (manifest + blobs) as `ollama pull` leaves it; its digest."""
    (store / "blobs").mkdir(parents=True, exist_ok=True)
    entries = []
    for content in layers.values():
        hexd = hashlib.sha256(content).hexdigest()
        (store / "blobs" / f"sha256-{hexd}").write_bytes(content)
        entries.append({"digest": f"sha256:{hexd}", "size": len(content)})
    manifest = json.dumps(
        {"schemaVersion": 2, "config": entries[0], "layers": entries[1:]}
    ).encode()
    path = store / "manifests" / installed.REGISTRY / "library" / name / tag
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest)
    return hashlib.sha256(manifest).hexdigest()


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with its own Ollama store; nothing from the real machine leaks in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf"))
    return home


def test_the_scan_lists_newtons_store_and_the_hosts_ollama(host: Path, tmp_path: Path) -> None:
    root = tmp_path / "worker"
    mine = ollama_model(
        root / "models" / "ollama", "llama3.2", "3b", {"c": b"cfg", "w": b"w" * 1000}
    )
    theirs = ollama_model(
        host / ".ollama" / "models", "gemma4", "latest", {"c": b"c2", "w": b"x" * 500}
    )
    found = {(e["model"], e["where"]): e for e in installed.scan(root)}
    assert found[("llama3.2:3b", "newton")]["revision"] == mine  # sha256 of the manifest
    assert found[("llama3.2:3b", "newton")]["ready"] is True
    assert found[("llama3.2:3b", "newton")]["size_bytes"] == 3 + 1000
    gemma = found[("gemma4:latest", "ollama")]
    assert gemma["revision"] == theirs and gemma["ready"] is True  # same disk, my files
    assert gemma["memory_gb_hint"] >= 1


def test_a_model_that_cant_be_linked_is_listed_but_needs_a_download(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ollama_model(host / ".ollama" / "models", "gemma4", "latest", {"c": b"c", "w": b"w"})
    monkeypatch.setattr(installed, "_linkable", lambda src, dst: False)  # another user's files
    (entry,) = installed.scan(tmp_path / "worker")
    assert entry["where"] == "ollama" and entry["ready"] is False


def test_reuse_hard_links_the_blobs_and_the_manifest_comes_last(host: Path, tmp_path: Path) -> None:
    root = tmp_path / "worker"
    digest = ollama_model(
        host / ".ollama" / "models", "gemma4", "latest", {"c": b"c", "w": b"w" * 64}
    )
    spec = svc.validate_spec({"service_id": "g", "engine": "ollama", "model": "gemma4:latest",
                              "revision": digest[:12], "memory_gb": 3})  # fmt: skip
    assert not svc.model_present(spec, root)
    assert installed.reuse_ollama("gemma4:latest", digest[:12], root)
    assert svc.model_present(spec, root)  # Newton's own store has it now
    src = host / ".ollama" / "models" / "blobs"
    for blob in src.iterdir():
        assert (
            os.stat(blob).st_ino == os.stat(root / "models" / "ollama" / "blobs" / blob.name).st_ino
        )


def test_another_revision_is_never_reused(host: Path, tmp_path: Path) -> None:
    ollama_model(host / ".ollama" / "models", "gemma4", "latest", {"c": b"c", "w": b"w"})
    assert installed.find_reusable("gemma4:latest", "0" * 12, tmp_path / "worker") is None
    assert installed.reuse_ollama("gemma4:latest", "0" * 12, tmp_path / "worker") is False


def test_admission_needs_no_download_for_a_reusable_model(host: Path, tmp_path: Path) -> None:
    digest = ollama_model(host / ".ollama" / "models", "gemma4", "latest", {"c": b"c", "w": b"w"})
    spec = svc.validate_spec({"service_id": "g", "engine": "ollama", "model": "gemma4:latest",
                              "revision": digest[:12], "memory_gb": 1})  # fmt: skip
    out = svc.ServiceStore(tmp_path / "worker").admission(spec)
    assert out["model_present"] is True and out["reused_from"] == "ollama"


def test_mlx_snapshots_in_the_hugging_face_cache(host: Path, tmp_path: Path) -> None:
    commit = "a" * 40
    snap = (
        tmp_path / "hf" / "models--mlx-community--Qwen2.5-0.5B-Instruct-4bit" / "snapshots" / commit
    )
    snap.mkdir(parents=True)
    (snap / "model.safetensors").write_bytes(b"0" * 2048)
    (entry,) = installed.scan(tmp_path / "worker")
    assert entry == {"engine": "mlx", "model": "mlx-community/Qwen2.5-0.5B-Instruct-4bit",
                     "revision": commit, "size_bytes": 2048, "memory_gb_hint": 2.0,
                     "where": "huggingface", "ready": False}  # fmt: skip
