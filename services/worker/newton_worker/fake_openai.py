"""A fake OpenAI-compatible model server, for tests and selftests of services:
`python -m newton_worker.fake_openai --port N --model M [options]`.

GET /health (503 while "loading"), GET /v1/models, POST /v1/chat/completions
(echoes the last message; `stream: true` sends it word by word as server-sent events),
/v1/completions, /v1/embeddings (deterministic vectors). GET /stats reports the requests
seen, in flight now, the most ever in flight at once, and streams the client dropped.
Options simulate the hard parts of real engines: a slow start, slow answers, memory
held, a crash, health turning bad, ignoring SIGTERM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--startup-delay", type=float, default=0.0)
    ap.add_argument("--hold-memory-mb", type=int, default=0)
    ap.add_argument("--crash-after", type=float, default=0.0)
    ap.add_argument("--unhealthy-after", type=float, default=0.0)
    ap.add_argument("--ignore-sigterm", action="store_true")
    ap.add_argument("--reply-delay", type=float, default=0.0)
    ap.add_argument("--fail-status", type=int, default=0)  # answer chats with this error
    ap.add_argument("--stream-cut-after", type=int, default=0)  # end streams early
    ap.add_argument("--reply-file", default="")  # answer chats with this text (tests)
    args = ap.parse_args()
    started = time.time()
    held = bytearray(args.hold_memory_mb * 1024 * 1024)  # touch it: really resident
    for i in range(0, len(held), 4096):
        held[i] = 1
    if args.ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if args.crash_after:
        threading.Timer(args.crash_after, lambda: os._exit(3)).start()
    stats: dict[str, Any] = {"count": 0, "active": 0, "max_active": 0, "dropped": 0}
    lock = threading.Lock()
    api_key = os.environ.get("NEWTON_FAKE_API_KEY")  # like vLLM's VLLM_API_KEY

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:
            print(fmt % a, flush=True)

        def reply(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            if not api_key or not self.path.startswith("/v1/"):
                return True
            if self.headers.get("Authorization") == f"Bearer {api_key}":
                return True
            self.reply(401, {"error": {"message": "invalid api key"}})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if not self.authorized():
                return
            loading = time.time() - started < args.startup_delay
            if self.path == "/health":
                bad = args.unhealthy_after and time.time() - started > args.unhealthy_after
                if loading:
                    self.reply(503, {"status": "loading"})
                elif bad:
                    self.reply(500, {"status": "unhealthy"})
                else:
                    self.reply(200, {"status": "ok"})
            elif self.path == "/stats":
                with lock:
                    self.reply(200, dict(stats))
            elif self.path == "/v1/models":
                self.reply(200, {"object": "list", "data": [{"id": args.model, "object": "model"}]})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self.authorized():
                return
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            )
            answer = {"/v1/chat/completions": self.chat, "/v1/completions": self.complete,
                      "/v1/embeddings": self.embed}.get(self.path)  # fmt: skip
            if answer is None:
                self.reply(404, {"error": "not found"})
                return
            with lock:
                stats["last_body"] = {k: body[k] for k in ("model",) if k in body}
                stats["last_keys"] = sorted(body)
                stats["count"] += 1
                stats["active"] += 1
                stats["max_active"] = max(stats["max_active"], stats["active"])
                n = stats["count"]
            try:
                answer(body, n)
            finally:
                with lock:
                    stats["active"] -= 1

        def chat(self, body: dict[str, Any], n: int) -> None:
            last = str((body.get("messages") or [{}])[-1].get("content", ""))
            if args.fail_status:  # like vLLM's validation errors, it echoes the input
                self.reply(args.fail_status, {"error": {"message": f"bad input: {last}",
                                                        "type": "BadRequestError"}})  # fmt: skip
                return
            text = f"echo: {last}"
            if args.reply_file:
                with open(args.reply_file, encoding="utf-8") as f:
                    text = f.read()
            words = text.split(" ")
            usage = {"prompt_tokens": len(last.split()), "completion_tokens": len(words),
                     "total_tokens": len(last.split()) + len(words)}  # fmt: skip
            if not body.get("stream"):
                time.sleep(args.reply_delay)
                self.reply(200, {
                    "id": f"fake-{n}", "object": "chat.completion", "model": args.model,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": " ".join(words)}}],
                    "usage": usage,
                })  # fmt: skip
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunk = {"id": f"fake-{n}", "object": "chat.completion.chunk", "model": args.model}
            events = [{**chunk, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
            events += [{**chunk, "choices": [{"index": 0, "delta": {
                "content": w if i == 0 else " " + w}}]} for i, w in enumerate(words)]  # fmt: skip
            events.append({**chunk, "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "stop"}]})  # fmt: skip
            if (body.get("stream_options") or {}).get("include_usage"):
                events.append({**chunk, "choices": [], "usage": usage})
            if args.stream_cut_after:
                events = events[: args.stream_cut_after]
            try:
                for event in events:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(args.reply_delay / len(events))
                if not args.stream_cut_after:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                with lock:
                    stats["dropped"] += 1
            self.close_connection = True

        def complete(self, body: dict[str, Any], n: int) -> None:
            time.sleep(args.reply_delay)
            prompt = str(body.get("prompt", ""))
            self.reply(200, {
                "id": f"fake-{n}", "object": "text_completion", "model": args.model,
                "choices": [{"index": 0, "text": f"echo: {prompt}", "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt.split()), "completion_tokens": 2,
                          "total_tokens": len(prompt.split()) + 2},
            })  # fmt: skip

        def embed(self, body: dict[str, Any], n: int) -> None:
            time.sleep(args.reply_delay)
            texts = body.get("input", "")
            texts = [texts] if isinstance(texts, str) else [str(t) for t in texts]
            data = [{"object": "embedding", "index": i,
                     "embedding": [b / 255 for b in hashlib.sha256(t.encode()).digest()[:8]]}
                    for i, t in enumerate(texts)]  # fmt: skip
            tokens = sum(len(t.split()) for t in texts)
            usage = {"prompt_tokens": tokens, "total_tokens": tokens}
            self.reply(200, {"object": "list", "data": data, "model": args.model, "usage": usage})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fake model server for {args.model} on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
