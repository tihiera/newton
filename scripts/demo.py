"""Drive the backend demo flow through the HTTP API (what the UI will do).

    scripts/dev.sh                      # terminal 1
    uv run python scripts/demo.py       # terminal 2  [--host <host_id>] [--backend gpu]

Creates a goal and an experiment, approves it, streams job progress, and prints
the report location.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=f"http://127.0.0.1:{os.environ.get('NEWTON_PORT', 8765)}")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(
            os.environ.get("NEWTON_DATA_DIR", Path(__file__).resolve().parents[1] / ".data")
        ),
    )
    parser.add_argument("--host", default="local")
    parser.add_argument("--backend", choices=["cpu", "gpu"], default="cpu")
    args = parser.parse_args()

    token = (args.data_dir / "api-token").read_text().strip()
    c = httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    print("health:", c.get("/health").json()["status"])
    host = c.post(f"/hosts/{args.host}/check").json()
    print(f"host {host['name']}: {host['status']}, gpus={len(host['hardware'].get('gpus', []))}")

    goal = c.post(
        "/goals",
        json={
            "title": "Higher-order advection without losing conservation",
            "keywords": ["advection", "TVD", "MUSCL", "flux limiter"],
        },
    ).json()
    res = {"resolutions": [128, 256, 512, 1024, 2048], "repeats": 3}
    exp = c.post(
        "/experiments",
        json={
            "title": "Upwind vs second-order schemes (1D linear advection)",
            "goal_id": goal["id"],
            "host_id": args.host,
            "backend": args.backend,
            "hypothesis": "Flux-limited second-order schemes reduce L2 error by >2x at the same "
            "resolution while staying conservative and non-oscillatory.",
            "variants": [
                {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **res}},
                {
                    "role": "candidate",
                    "label": "lax-wendroff",
                    "params": {"scheme": "lax_wendroff", **res},
                },
                {
                    "role": "candidate",
                    "label": "muscl-vanleer",
                    "params": {"scheme": "muscl_vanleer", **res},
                },
            ],
        },
    ).json()
    print(f"experiment {exp['id']}: {exp['state']}")

    approval = next(
        a for a in c.get("/approvals?status=pending").json() if a["subject_id"] == exp["id"]
    )
    print(f"approving: {approval['title']}")
    c.post(f"/approvals/{approval['id']}/approve", json={"note": "demo"})

    offsets: dict[str, int] = {}
    while True:
        e = c.get(f"/experiments/{exp['id']}").json()
        for j in e["jobs"]:
            chunk = c.get(
                f"/jobs/{j['id']}/logs", params={"offset": offsets.get(j["id"], 0)}
            ).json()
            for line in chunk["data"].splitlines():
                print(f"  [{j['label']}] {line}")
            offsets[j["id"]] = chunk["next_offset"]
        if e["state"] in ("reported", "failed", "cancelled"):
            break
        time.sleep(0.5)

    print(f"\nstate: {e['state']}  evidence: {e['evidence']}")
    if e["evaluation"]:
        print(e["evaluation"]["summary"])
    print(f"report: {e['report_path']}")
    return 0 if e["state"] == "reported" else 1


if __name__ == "__main__":
    sys.exit(main())
