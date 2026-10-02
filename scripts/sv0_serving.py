"""SV0: measure model serving on a GPU host (run *on* the host; stdlib only).

    python3 sv0_serving.py --model gpt-oss:120b [--url http://127.0.0.1:11434]
        [--ctx 8192] [--tokens 256] [--concurrency 1,8,32] [--skew-cmd "..."]

Against an Ollama server bound to 127.0.0.1. Measures, and prints one JSON line:
  - cold start: load from disk to the first token (model unloaded first);
  - warm start: the same request with the model resident;
  - decode throughput at each concurrency: aggregate tokens/s over the wall time,
    per-request decode tokens/s, and client-side time to first token (which
    includes queueing when there are more requests than server slots);
  - optionally, whether an idle loaded model slows a GPU benchmark (--skew-cmd is
    run 3x with the model resident and 3x after unloading; its last stdout line
    must contain "ms/step").
Unloads the model at the end (servers often keep models resident for hours).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import threading
import time
import urllib.request
from typing import Any

PROMPTS = [
    "Explain, step by step, how a finite-volume scheme conserves mass on a periodic grid.",
    "Write a careful comparison of upwind and Lax-Wendroff schemes for linear advection.",
    "Describe how flux limiters keep a second-order scheme free of new extrema.",
    "Summarize the trade-offs of dimension splitting for 2D advection problems.",
]


# Local URLs only: never through a proxy from the environment.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url: str, body: dict[str, Any], stream: bool = False, timeout: float = 1800) -> Any:
    req = urllib.request.Request(  # noqa: S310 - main() only accepts http://127.0.0.1
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    return DIRECT.open(req, timeout=timeout)


def generate(base: str, model: str, prompt: str, tokens: int, ctx: int) -> dict[str, Any]:
    """One streamed request; client-side timings plus the server's own stats."""
    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"num_ctx": ctx, "num_predict": tokens, "temperature": 0, "seed": 7},
    }
    t0 = time.perf_counter()
    first = None
    final: dict[str, Any] = {}
    with post(f"{base}/api/generate", body, stream=True) as resp:
        for line in resp:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if first is None and (chunk.get("response") or chunk.get("thinking")):
                first = time.perf_counter() - t0
            if chunk.get("done"):
                final = chunk
    wall = time.perf_counter() - t0
    eval_s = (final.get("eval_duration") or 0) / 1e9
    return {
        "wall_s": wall,
        "ttft_s": first,
        "tokens": final.get("eval_count") or 0,
        "decode_tok_s": (final.get("eval_count") or 0) / eval_s if eval_s else None,
        "prompt_tokens": final.get("prompt_eval_count"),
        "load_s": (final.get("load_duration") or 0) / 1e9,
    }


def unload(base: str, model: str) -> None:
    with post(f"{base}/api/generate", {"model": model, "keep_alive": 0}) as resp:
        resp.read()


def loaded(base: str) -> list[dict[str, Any]]:
    with DIRECT.open(f"{base}/api/ps", timeout=30) as resp:
        return list(json.load(resp).get("models") or [])


def concurrent(base: str, model: str, n: int, tokens: int, ctx: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        prompt = f"[{i}] {PROMPTS[i % len(PROMPTS)]}"  # distinct: no prompt-cache reuse
        r = generate(base, model, prompt, tokens, ctx)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    total = sum(r["tokens"] for r in results)
    ttft = [r["ttft_s"] for r in results if r["ttft_s"] is not None]
    decode = [r["decode_tok_s"] for r in results if r["decode_tok_s"]]
    return {
        "concurrency": n,
        "wall_s": round(wall, 2),
        "tokens": total,
        "aggregate_tok_s": round(total / wall, 1),
        "per_request_decode_tok_s_median": round(statistics.median(decode), 1) if decode else None,
        "ttft_s_median": round(statistics.median(ttft), 2) if ttft else None,
        "ttft_s_max": round(max(ttft), 2) if ttft else None,
    }


def gpu_snapshot() -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,clocks.sm,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()  # fmt: skip
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name,used_memory",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return {}
    util, clock, power = (x.strip() for x in out.split(","))
    return {"utilization_pct": util, "sm_mhz": clock, "power_w": power, "compute_apps": apps}


def skew(command: str, base: str, model: str, tokens: int, ctx: int) -> dict[str, Any]:
    def run3() -> list[float]:
        times = []
        for _ in range(3):
            out = subprocess.run(command, shell=True, capture_output=True, text=True,  # noqa: S602
                                 timeout=1800).stdout.strip().splitlines()  # fmt: skip
            line = next(ln for ln in reversed(out) if "ms/step" in ln)
            times.append(float(line.split("done:")[1].split("ms/step")[0]))
        return times

    generate(base, model, PROMPTS[0], 1, ctx)  # resident, then idle
    resident = run3()
    unload(base, model)
    time.sleep(5)
    unloaded = run3()
    return {
        "ms_per_step_model_resident": resident,
        "ms_per_step_model_unloaded": unloaded,
        "slowdown": round(statistics.median(resident) / statistics.median(unloaded), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--tokens", type=int, default=256)
    ap.add_argument("--concurrency", default="1,8,32")
    ap.add_argument("--skew-cmd")
    args = ap.parse_args()
    base, model = args.url.rstrip("/"), args.model
    if not base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise SystemExit("--url must be a local http server (http://127.0.0.1:<port>)")

    out: dict[str, Any] = {"model": model, "ctx": args.ctx, "tokens": args.tokens,
                           "gpu_before": gpu_snapshot()}  # fmt: skip
    unload(base, model)
    time.sleep(3)
    cold = generate(base, model, PROMPTS[1], 1, args.ctx)
    warm = generate(base, model, PROMPTS[2], 1, args.ctx)
    out["cold_start"] = {"wall_s": round(cold["wall_s"], 2), "load_s": round(cold["load_s"], 2)}
    out["warm_start"] = {"wall_s": round(warm["wall_s"], 2), "ttft_s": warm["ttft_s"]}
    out["resident"] = [
        {k: m.get(k) for k in ("name", "size", "size_vram", "context_length")} for m in loaded(base)
    ]
    out["throughput"] = []
    for n in (int(x) for x in args.concurrency.split(",")):
        out["throughput"].append(concurrent(base, model, n, args.tokens, args.ctx))
        print(json.dumps(out["throughput"][-1]), flush=True)
    out["gpu_after_load"] = gpu_snapshot()
    if args.skew_cmd:
        out["skew"] = skew(args.skew_cmd, base, model, args.tokens, args.ctx)
    unload(base, model)
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
