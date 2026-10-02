"""Worker self-test job: `python -m newton_worker.selftest [--sleep S] [--fail]`.

Writes results.json (hardware + a tiny compute check) and artifacts/hello.txt.
Used to verify the full submit → run → collect path on a new host.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .hardware import probe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()

    print(f"selftest starting (job={os.environ.get('NEWTON_JOB_ID')})", flush=True)
    t0 = time.perf_counter()
    total = sum(i * i for i in range(200_000))
    if args.sleep:
        steps = max(1, int(args.sleep / 0.1))
        for i in range(steps):
            time.sleep(args.sleep / steps)
            if i % 10 == 0:
                print(f"tick {i}/{steps}", flush=True)
    if args.fail:
        raise SystemExit("selftest asked to fail")

    out = Path("artifacts")
    out.mkdir(exist_ok=True)
    (out / "hello.txt").write_text("hello from the newton worker\n")
    results = {
        "metrics": {"checksum": total, "runtime": time.perf_counter() - t0},
        "hardware": probe(Path(".")),
        "backend": os.environ.get("NEWTON_BACKEND"),
    }
    Path("results.json").write_text(json.dumps(results, indent=2))
    print("selftest done", flush=True)


if __name__ == "__main__":
    main()
