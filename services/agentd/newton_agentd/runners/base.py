"""Runner interface. Every transport ends in the same worker HTTP protocol."""

from __future__ import annotations

from typing import Any, Protocol


class RunnerError(Exception):
    """Transport or worker failure. `transient` errors are retried."""

    def __init__(self, message: str, *, transient: bool = True, code: str = "error") -> None:
        super().__init__(message)
        self.transient = transient
        self.code = code


class Runner(Protocol):
    host_id: str

    async def ensure_ready(self) -> None:
        """Make the worker reachable (start local worker / open SSH tunnel)."""

    async def health(self) -> dict[str, Any]: ...
    async def hardware(self) -> dict[str, Any]: ...
    async def submit(self, manifest: dict[str, Any], bundle: bytes) -> dict[str, Any]: ...
    async def status(self, remote_id: str) -> dict[str, Any]: ...
    async def logs(self, remote_id: str, stream: str, offset: int) -> dict[str, Any]: ...
    async def artifacts(self, remote_id: str) -> bytes: ...
    async def cancel(self, remote_id: str) -> dict[str, Any]: ...
    async def close(self) -> None: ...
