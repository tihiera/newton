"""FastAPI application factory, auth and error mapping."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .api import routes
from .config import Settings
from .context import AppContext
from .errors import NotFound
from .orchestration.approvals import ApprovalAlreadyDecided, ApprovalNotFound
from .orchestration.hosts import GpuSupportBusy, HostKeyUnknown, HostNotFound
from .orchestration.jobs import JobNotFound
from .orchestration.state_machine import ConcurrentTransition, IllegalTransition
from .research.experiment_design import ExperimentNotFound
from .runners.base import RunnerError
from .secrets import SecretStore
from .serving.manager import ServiceNotFound, ServiceRefused
from .serving.router import RouterError

PUBLIC_PATHS = {"/health"}
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "testserver"}
# Origins of the Tauri webview (macOS) and the Vite dev server.
UI_ORIGINS = ["tauri://localhost", "http://tauri.localhost", "http://localhost:1420"]


class Guard:
    """Loopback host names only (DNS rebinding) and a bearer token on everything but
    /health. /v1/* (the router) also takes the router key, which is inference only,
    and answers in the error shape OpenAI clients understand.

    Plain ASGI rather than BaseHTTPMiddleware: the router must see a client's
    disconnect (http.disconnect) to free its slot and stop the engine."""

    def __init__(self, app: ASGIApp, settings: Settings, ctx: AppContext) -> None:
        self.app = app
        self.settings = settings
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path: str = scope["path"]
        v1 = path.startswith("/v1/") or path == "/v1"
        hostname = (headers.get("host") or "").rsplit(":", 1)[0]
        response: Response | None = None
        if hostname not in ALLOWED_HOSTNAMES:
            response = (
                RouterError(403, "forbidden host", "invalid_request_error", "forbidden_host")
                .response() if v1 else JSONResponse({"error": "forbidden host"}, status_code=403)
            )  # fmt: skip
        elif scope["method"] != "OPTIONS" and path not in PUBLIC_PATHS:
            scheme, _, token = headers.get("authorization", "").partition(" ")
            given = token.encode("latin-1")  # bytes: any header value compares safely
            bearer = scheme.lower() == "bearer"
            ok = bearer and hmac.compare_digest(given, self.settings.resolve_api_token().encode())
            if not ok and v1 and bearer:
                # Only the cached key: a secret-store read could block, or prompt.
                router_key = self.ctx.profile.cached_router_key()
                if router_key is None:
                    response = RouterError(
                        503, "the router key is unavailable (is the keychain locked?)",
                        "server_error", "secret_store", retry_after=10,
                    ).response()  # fmt: skip
                else:
                    ok = hmac.compare_digest(given, router_key.encode())
            if not ok and response is None:
                response = (
                    RouterError(401, "invalid API key", "invalid_request_error",
                                "invalid_api_key").response()
                    if v1 else JSONResponse({"error": "unauthorized"}, status_code=401)
                )  # fmt: skip
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None, secret_store: SecretStore | None = None
) -> FastAPI:
    settings = settings or Settings()
    ctx = AppContext.create(settings, secret_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title="Newton agentd", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx

    app.add_middleware(Guard, settings=settings, ctx=ctx)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=UI_ORIGINS,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def handler(status: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handle(_: Request, exc: Exception) -> JSONResponse:
            body: dict[str, Any] = {"error": str(exc).strip("'\"") or type(exc).__name__}
            if isinstance(exc, RunnerError):
                body["code"] = exc.code
            if isinstance(exc, HostKeyUnknown):
                body["fingerprints"] = exc.keys
            return JSONResponse(body, status_code=status)

        return handle

    for exc_type, status in (
        (HostKeyUnknown, 409),
        (ServiceRefused, 409),
        (ServiceNotFound, 404),
        (GpuSupportBusy, 409),
        (NotFound, 404),
        (HostNotFound, 404),
        (JobNotFound, 404),
        (ExperimentNotFound, 404),
        (ApprovalNotFound, 404),
        (ApprovalAlreadyDecided, 409),
        (IllegalTransition, 409),
        (ConcurrentTransition, 409),
        (RunnerError, 502),
        (ValueError, 400),
    ):
        app.add_exception_handler(exc_type, handler(status))

    app.include_router(routes.router)
    return app
