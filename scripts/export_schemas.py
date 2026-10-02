"""Write packages/contracts/*.schema.json from the pydantic contracts.

Usage: uv run python scripts/export_schemas.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from newton_agentd.contracts import SCHEMAS

OUT = Path(__file__).resolve().parents[1] / "packages" / "contracts"


def render() -> dict[str, str]:
    return {
        f"{name}.schema.json": json.dumps(model.model_json_schema(), indent=2, sort_keys=True)
        + "\n"
        for name, model in SCHEMAS.items()
    }


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for filename, content in render().items():
        path = OUT / filename
        if check:
            if not path.exists() or path.read_text() != content:
                stale.append(filename)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        print("stale contract schemas: " + ", ".join(stale), file=sys.stderr)
        print("run: uv run python scripts/export_schemas.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
