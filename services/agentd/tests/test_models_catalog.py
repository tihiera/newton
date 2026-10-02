"""Choosing a model instead of typing one: the starter catalog (pinned digests) and the
models a machine already has (the worker's scan, through agentd)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import AUTH
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings


def test_the_catalog_is_pinned_and_sized(client: TestClient) -> None:
    models = client.get("/models/catalog").json()
    assert len(models) == 10
    for m in models:
        assert m["engine"] == "ollama" and ":" in m["model"]
        assert re.fullmatch(r"[0-9a-f]{64}", m["revision"])  # a manifest digest, not a tag
        assert m["size_bytes"] > 1e9 and m["memory_gb_hint"] > m["size_bytes"] / 1024**3
        assert m["note"]
    names = {m["model"] for m in models}
    assert {"llama3.2:3b", "gemma4:latest", "nemotron-mini:4b"} <= names


def test_a_machine_lists_what_it_already_has(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This Mac's worker starts with agentd and scans the user's own stores: point them
    # at an empty home first.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf"))
    snap = tmp_path / "hf" / "models--mlx-community--Qwen2.5-0.5B-Instruct-4bit" / "snapshots"
    (snap / ("b" * 40)).mkdir(parents=True)
    (snap / ("b" * 40) / "model.safetensors").write_bytes(b"0" * 100)
    with TestClient(create_app(settings), headers=AUTH) as client:
        models = client.get("/hosts/local/models").json()
        assert [(m["engine"], m["model"], m["where"]) for m in models] == [
            ("mlx", "mlx-community/Qwen2.5-0.5B-Instruct-4bit", "huggingface")
        ]
        assert client.get("/hosts/nope/models").status_code == 404
