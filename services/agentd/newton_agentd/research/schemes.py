"""Schemes as data (engine E2), seen from agentd.

The rules live once, in benchmarks/advection/ir.py (the job runs them too): agentd
loads that module from the benchmarks it bundles, never from the job's results, to
validate a SchemeIR document, pin its digest and the hash of the code it generates
before approval, and to serve the library of known schemes.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

_loaded: dict[tuple[Path, str], ModuleType] = {}


def ir_module(benchmarks_dir: Path) -> ModuleType:
    """ir.py as the bundles carry it: reloaded when the file changes, so what agentd
    pins (digests, generated code hashes) is what the job will compute."""
    path = (benchmarks_dir / "advection" / "ir.py").resolve()
    key = (path, hashlib.sha256(path.read_bytes()).hexdigest())
    if key not in _loaded:
        spec = importlib.util.spec_from_file_location("newton_scheme_ir", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"can't load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded[key] = module
    return _loaded[key]


def check(benchmarks_dir: Path, doc: dict[str, Any]) -> dict[str, Any]:
    """The canonical document, its digest, and the generated header's hash (what an
    approval pins); ValueError when the document breaks a rule."""
    ir = ir_module(benchmarks_dir)
    try:
        canonical = ir.validate(doc)
    except ir.IRError as e:
        raise ValueError(f"scheme_ir: {e}") from None
    return {
        "document": canonical,
        "digest": ir.digest(canonical),
        "method_digest": ir.method_digest(canonical),
        "header": f"ir:{canonical['name']}.h",
        "header_hash": ir.header_hash(canonical),
    }


def library(benchmarks_dir: Path) -> list[dict[str, Any]]:
    ir = ir_module(benchmarks_dir)
    out = []
    for name, doc in ir.LIBRARY.items():
        canonical = ir.validate(doc)
        out.append({"name": name, "document": canonical, "digest": ir.digest(canonical),
                    "hand_written": name in ("upwind", "lax_wendroff", "muscl_minmod",
                                             "muscl_vanleer")})  # fmt: skip
    return out
