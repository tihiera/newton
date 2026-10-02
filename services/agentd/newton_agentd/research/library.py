"""Built-in schemes against a baseline, with no paper (B7).

`library_proposal()`: library schemes (the /schemes library, as SchemeIR documents)
against a baseline in a grid-refinement study, like `papers.proposal()` for a card
but without a paper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..storage.db import Database
from . import schemes

MAX_CANDIDATES = 7  # an experiment has at most 8 variants, one of them the baseline
MAX_CFL = 0.8  # what a card's proposal runs at, at most (papers.proposal)


def library_proposal(benchmarks_dir: Path, db: Database, candidates: list[str],
                     baseline: str, initial_condition: str, host_id: str, backend: str,
                     goal_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """An ExperimentSpec (as a dict) comparing library schemes with a baseline;
    ValueError for unknown names or a goal that doesn't exist."""
    library = {s["name"]: s for s in schemes.library(benchmarks_dir)}
    chosen = list(dict.fromkeys(candidates))  # the same scheme twice: once
    unknown = [n for n in [*chosen, baseline] if n not in library]
    if unknown:
        raise ValueError(f"no scheme named {', '.join(repr(n[:64]) for n in unknown)} in "
                         f"Newton's library (it has {', '.join(library)})")  # fmt: skip
    if baseline in chosen:
        raise ValueError(f"{baseline} is the baseline: it can't also be a candidate")
    if len(chosen) > MAX_CANDIDATES:
        raise ValueError(f"at most {MAX_CANDIDATES} candidates in one experiment")
    if goal_id and not db.query_one("SELECT 1 FROM goals WHERE id = ?", (goal_id,)):
        raise ValueError(f"unknown goal {goal_id}")
    # Same problem for every variant (only the scheme may vary): the step the most
    # restrictive scheme claims to be stable at.
    cfl = min(MAX_CFL, *(float(library[n]["document"]["claims"]["max_cfl"])
                         for n in [baseline, *chosen]))  # fmt: skip
    common = {"resolutions": [64, 128, 256, 512, 1024], "initial_condition": initial_condition,
              "cfl": cfl}  # fmt: skip

    base = library[baseline]  # a hand-written baseline runs as itself, as for a paper
    base_params = ({"scheme": baseline, **common} if base["hand_written"]
                   else {"scheme": "ir", "scheme_ir": base["document"], **common})  # fmt: skip

    return {
        "title": f"{', '.join(chosen)} vs {baseline} (built-in schemes)"[:200],
        "host_id": host_id,
        "backend": backend,
        "goal_id": goal_id,
        "hypothesis": (f"Each built-in scheme's claims (order, largest stable CFL, TVD) hold "
                       f"in a grid-refinement study against {baseline}."),  # fmt: skip
        "variants": [
            {"role": "baseline", "label": baseline, "params": base_params},
            *({"role": "candidate", "label": name,
               "params": {"scheme": "ir", "scheme_ir": library[name]["document"], **common}}
              for name in chosen),
        ],
    }  # fmt: skip
