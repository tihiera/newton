"""Async HTTP client for the worker protocol (see newton_worker.server)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from .base import RunnerError

T = TypeVar("T")


class WorkerClient:
    def __init__(
        self, base_url: str, token: str, timeout: float = 30.0, uds: str | None = None
    ) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=uds) if uds else None,
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=False,  # never through an HTTP proxy: it would see the worker token
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise RunnerError(f"worker unreachable: {e!r}", code="unreachable") from e
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", resp.text)
            except ValueError:
                message = resp.text
            # 4xx means the worker rejected the request: retrying won't help.
            raise RunnerError(
                f"worker {method} {path} → {resp.status_code}: {message}",
                transient=resp.status_code >= 500 or resp.status_code == 401,
                code="unauthorized" if resp.status_code == 401 else f"http_{resp.status_code}",
            )
        return resp

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return (await self._request(method, path, **kwargs)).json()

    async def health(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("GET", "/health")
        return result

    async def hardware(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("GET", "/hardware", timeout=60)
        return result

    async def submit(self, manifest: dict[str, Any], bundle: bytes) -> dict[str, Any]:
        """create → upload bundle → start. Each step is idempotent on job_id."""
        job_id = manifest["job_id"]
        status = await self._json("POST", "/jobs", json=manifest)
        if status["state"] == "created":
            status = await self._json(
                "PUT",
                f"/jobs/{job_id}/bundle",
                content=bundle,
                headers={"Content-Type": "application/gzip"},
                timeout=300,
            )
        if status["state"] == "bundled":
            status = await self._json("POST", f"/jobs/{job_id}/start")
        result: dict[str, Any] = status
        return result

    async def status(self, remote_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("GET", f"/jobs/{remote_id}")
        return result

    async def logs(self, remote_id: str, stream: str, offset: int) -> dict[str, Any]:
        result: dict[str, Any] = await self._json(
            "GET", f"/jobs/{remote_id}/logs", params={"stream": stream, "offset": offset}
        )
        return result

    async def artifacts(self, remote_id: str) -> bytes:
        return (await self._request("GET", f"/jobs/{remote_id}/artifacts", timeout=600)).content

    async def cancel(self, remote_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("POST", f"/jobs/{remote_id}/cancel")
        return result


class WorkerClientRunner:
    """Shared Runner implementation on top of a WorkerClient; subclasses
    implement `ensure_ready` to establish `self.client`."""

    host_id: str
    client: WorkerClient | None = None

    def _c(self) -> WorkerClient:
        if self.client is None:
            raise RunnerError("runner not ready", code="not_ready")
        return self.client

    async def ensure_ready(self) -> None:
        raise NotImplementedError

    async def _call(self, op: Callable[[WorkerClient], Awaitable[T]]) -> T:
        """Run op; if the worker is unreachable (dead tunnel, crashed worker),
        reconnect once and retry. Every worker operation is idempotent."""
        await self.ensure_ready()
        try:
            return await op(self._c())
        except RunnerError as e:
            if e.code != "unreachable":
                raise
        await self.reset()
        await self.ensure_ready()
        return await op(self._c())

    async def worker_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Any worker API call (e.g. /services), with the same reconnect-once rule."""
        return await self._call(lambda c: c._json(method, path, **kwargs))

    async def reset(self) -> None:
        """Drop the connection so the next ensure_ready reconnects."""
        await self.close()

    async def health(self) -> dict[str, Any]:
        return await self._call(lambda c: c.health())

    async def hardware(self) -> dict[str, Any]:
        return await self._call(lambda c: c.hardware())

    async def submit(self, manifest: dict[str, Any], bundle: bytes) -> dict[str, Any]:
        return await self._call(lambda c: c.submit(manifest, bundle))

    async def status(self, remote_id: str) -> dict[str, Any]:
        return await self._call(lambda c: c.status(remote_id))

    async def logs(self, remote_id: str, stream: str, offset: int) -> dict[str, Any]:
        return await self._call(lambda c: c.logs(remote_id, stream, offset))

    async def artifacts(self, remote_id: str) -> bytes:
        return await self._call(lambda c: c.artifacts(remote_id))

    async def cancel(self, remote_id: str) -> dict[str, Any]:
        return await self._call(lambda c: c.cancel(remote_id))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
