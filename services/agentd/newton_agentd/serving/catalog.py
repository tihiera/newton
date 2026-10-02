"""Models people can start with one click: well-known Ollama models, each pinned to the
manifest digest it had when this list was made (registry.ollama.ai, 2026-10-02), so a
tag that moves later is refused instead of silently running something else.

Sizes are the download (weights and config); `memory_gb_hint` adds room for the KV
cache, as the worker's scan does for models already on a machine.
"""

from __future__ import annotations

import math
from typing import Any

GIB = 1024**3

_MODELS: list[tuple[str, str, int, str]] = [
    # (model, manifest sha256, download bytes, what it's good for)
    ("llama3.2:3b", "a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72",
     2019393189, "Fast and small: reads papers on a laptop"),
    ("llama3.1:8b", "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e",
     4920753328, "A solid general reader"),
    ("qwen2.5:7b", "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e",
     4683087332, "Good at structured answers (research cards)"),
    ("qwen3:14b", "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8",
     9276198565, "Stronger reasoning, slower"),
    ("gemma4:latest", "dc35e8d9c6061baa6f0fa870975ab6932e2542b579b13ea0f199fa4bb7300c9c",
     6583656505, "Google's Gemma 4"),
    ("gemma3:12b", "f4031aab637d1ffa37b42570452ae0e4fad0314754d17ded67322e4b95836f8a",
     8149190253, "Google's Gemma 3, mid-size"),
    ("phi4:14b", "ac896e5b8b34a1f4efa7b14d7520725140d5512484457fab45d2a4ea14c69dba",
     9053116391, "Microsoft's Phi-4: strong on math"),
    ("nemotron-mini:4b", "ed76ab18784f5a01c9ec5b3c250e964d4f9c7a983e59ba041bb995f0fe2e8fb3",
     2697402546, "NVIDIA Nemotron, small"),
    ("nemotron:70b", "2262f047a28a348ebbdb44fd275f68907aa8916cac6775456348a60f93add589",
     42520412573, "NVIDIA Nemotron 70B: for a GPU box with lots of memory"),
    ("gpt-oss:20b", "17052f91a42e97930aa6e28a6c6c06a983e6a58dbb00434885a0cf5313e376f7",
     13793441244, "OpenAI's open-weight model"),
]  # fmt: skip


def memory_hint(size_bytes: int) -> float:
    """GB to declare: the weights plus room for the KV cache (the worker's rule too)."""
    return float(max(1, math.ceil(size_bytes / GIB * 1.2 + 1)))


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "engine": "ollama",
            "model": model,
            "revision": digest,
            "size_bytes": size,
            "memory_gb_hint": memory_hint(size),
            "note": note,
        }
        for model, digest, size, note in _MODELS
    ]
