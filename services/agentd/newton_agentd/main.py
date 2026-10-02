"""CLI: `newton-agentd serve [--port N] [--data-dir DIR] [--exit-on-stdin-eof]`.

On startup prints one JSON line the desktop shell can parse:
    {"event": "ready", "url": ..., "token_file": ...}

`--exit-on-stdin-eof`: the desktop shell holds agentd's stdin open; when the shell
goes away (even killed), stdin reaches EOF and agentd shuts down cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import uvicorn

from . import runtime_check
from .app import create_app
from .config import Settings
from .serving.router import Router

log = logging.getLogger("newton_agentd")

STDIN_FD = 0


class Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, router: Router) -> None:
        super().__init__(config)
        self.router = router

    def handle_exit(self, sig: int, frame: Any) -> None:
        # Before uvicorn waits for open requests: the router refuses new ones, empties
        # its queue and ends what's in flight (each client gets a "shutdown" error).
        self.router.begin_shutdown()
        super().handle_exit(sig, frame)

    def request_shutdown(self) -> None:
        """The same graceful shutdown as SIGTERM, without re-raising a signal after it
        (uvicorn does for captured signals): the exit status stays 0."""
        self.router.begin_shutdown()
        self.should_exit = True


def report_runtime(settings: Settings) -> None:
    """Loudly, never fatally: /health "runtime" carries the same sentences."""
    status = runtime_check.check(settings)
    for problem in status["problems"]:
        log.error("RUNTIME PROBLEM: %s", problem)
    if not runtime_check.numpy_available():
        log.error(
            "RUNTIME PROBLEM: %s; experiments can't run on this Mac", runtime_check.NUMPY_MISSING
        )
    log.info(
        "runtime: resources %s (%s)",
        settings.resources_dir,
        "ok" if status["resources_ok"] else "incomplete",
    )


def watch_stdin(loop: asyncio.AbstractEventLoop, server: Server) -> None:
    """A daemon thread reads stdin to EOF (the parent closed it or exited), then asks
    the server to stop: the same graceful shutdown as SIGTERM.

    It reads the raw file descriptor, never `sys.stdin.buffer`: a blocked buffered
    read holds the reader's lock, and any other exit (a port in use, Ctrl-C, a
    startup error) then aborts the interpreter when finalization closes sys.stdin
    ("could not acquire lock ... at interpreter shutdown", SIGABRT)."""

    def wait_for_eof() -> None:
        try:
            while os.read(STDIN_FD, 4096):
                pass
        except OSError:  # fd 0 closed or unreadable: the parent is gone all the same
            pass
        log.info("stdin closed: shutting down")
        try:
            loop.call_soon_threadsafe(server.request_shutdown)
        except RuntimeError:  # the loop already closed: agentd is exiting anyway
            pass

    threading.Thread(target=wait_for_eof, name="newton-stdin-eof", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(prog="newton-agentd")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int)
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="shut down when stdin reaches EOF (the desktop shell that started agentd is gone)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    settings = Settings()
    if args.port is not None:
        settings.port = args.port
    if args.data_dir is not None:
        settings.data_dir = args.data_dir.expanduser()
    report_runtime(settings)
    app = create_app(settings)

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        access_log=False,
        timeout_graceful_shutdown=10,  # open streams can't hold agentd up for ever
    )
    server = Server(config, app.state.ctx.router)

    async def run() -> None:
        if args.exit_on_stdin_eof:
            watch_stdin(asyncio.get_running_loop(), server)
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if server.started:
            port = server.servers[0].sockets[0].getsockname()[1]
            print(
                json.dumps(
                    {
                        "event": "ready",
                        "url": f"http://{settings.host}:{port}",
                        "token_file": str(settings.data_dir / "api-token"),
                        "data_dir": str(settings.data_dir),
                    }
                ),
                flush=True,
            )
        await task

    asyncio.run(run())


if __name__ == "__main__":
    main()
