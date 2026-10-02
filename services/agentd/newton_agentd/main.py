"""CLI: `newton-agentd serve [--port N] [--data-dir DIR]`.

On startup prints one JSON line the desktop shell can parse:
    {"event": "ready", "url": ..., "token_file": ...}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import uvicorn

from .app import create_app
from .config import Settings
from .serving.router import Router


class Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, router: Router) -> None:
        super().__init__(config)
        self.router = router

    def handle_exit(self, sig: int, frame: Any) -> None:
        # Before uvicorn waits for open requests: the router refuses new ones, empties
        # its queue and ends what's in flight (each client gets a "shutdown" error).
        self.router.begin_shutdown()
        super().handle_exit(sig, frame)


def main() -> None:
    parser = argparse.ArgumentParser(prog="newton-agentd")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int)
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    settings = Settings()
    if args.port is not None:
        settings.port = args.port
    if args.data_dir is not None:
        settings.data_dir = args.data_dir.expanduser()
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
