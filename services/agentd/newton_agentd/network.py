"""Is the Mac online? Learned from Newton's own outbound calls, never guessed.

    every call to the internet (arXiv, GitHub, Notion, a model API) -> note_ok(dest) when
    an answer came back (any status: the server was reached), note_failure(dest, exc)
    when it didn't -> offline when the most recent attempt failed at the connection
    level (no route, DNS, refused, timed out) and nothing succeeded since

State is "unknown" until the first call. A cheap probe (at most once a minute, only
while offline) lets background work notice the network is back without retrying
everything. Nothing here makes a request on its own: a probe is the caller's check.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger("newton_agentd.network")

DESTS = ("arxiv", "github", "notion", "models")
NAMES = {"arxiv": "arXiv", "github": "GitHub", "notion": "Notion", "models": "the model service"}
PROBE_EVERY = 60.0  # seconds between probes while offline
# Failures that say the network (or the far end) can't be reached, not that it answered.
OFFLINE_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    socket.gaierror,
    ConnectionError,
    TimeoutError,
)
NETWORK_ERRNOS = {errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH, errno.EHOSTDOWN}


def is_offline_error(exc: BaseException | None) -> bool:
    """A connection-level failure: no DNS, no route, refused, timed out (its causes too)."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, OFFLINE_ERRORS):
            return True
        if isinstance(exc, OSError) and exc.errno in NETWORK_ERRNOS:
            return True
        # `raise ... from None` hides the context: it isn't this failure's cause.
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        seen += 1
    return False


def _transport_error(exc: BaseException | None) -> bool:
    """An httpx transport failure (its causes too): the request went wrong on the way."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, httpx.TransportError):
            return True
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        seen += 1
    return False


def name(dest: str) -> str:
    return NAMES.get(dest, dest)


class NetworkState:
    """Passive: what Newton's last calls showed. Thread-safe (calls note from threads)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "unknown"
        self._since: float | None = None
        self._dest: str | None = None  # the destination that couldn't be reached
        self._last_probe = float("-inf")

    def note_ok(self, dest: str) -> None:
        """An answer came back from dest (whatever its status): the network is up."""
        with self._lock:
            if self._state != "online":
                if self._state == "offline":
                    log.info("the network is back (%s answered)", name(dest))
                self._state, self._since, self._dest = "online", self._clock(), None

    def note_failure(self, dest: str, exc: BaseException | None) -> bool:
        """A call to dest failed. True when the failure was connection-level (offline).
        A transport failure after connecting (the connection reset, a broken answer)
        says nothing either way: the state stays. Any other failure means the far end
        answered: it counts as online."""
        if not is_offline_error(exc):
            if not _transport_error(exc):
                self.note_ok(dest)
            return False
        with self._lock:
            if self._state != "offline":
                log.info("%s couldn't be reached (%s): working offline", name(dest),
                         type(exc).__name__)  # fmt: skip
                self._state, self._since = "offline", self._clock()
            self._dest = dest
        return True

    def is_offline(self) -> bool:
        with self._lock:
            return self._state == "offline"

    def snapshot(self) -> dict[str, Any]:
        """{state: online|offline|unknown, since: epoch|null, detail: sentence|null}"""
        with self._lock:
            state, since, dest = self._state, self._since, self._dest
        detail = None
        if state == "offline":
            detail = (f"{name(dest or 'arxiv')} couldn't be reached: Newton tries again "
                      "when the network is back")  # fmt: skip
        return {"state": state, "since": since, "detail": detail}

    async def probe(self, dest: str, check: Callable[[], Awaitable[Any]]) -> bool:
        """While offline, at most once every PROBE_EVERY seconds: run check (a cheap
        call to dest that raises when it can't be reached) and note how it went.
        Returns whether Newton is online (or unknown) afterwards."""
        with self._lock:
            if self._state != "offline":
                return True
            t = time.monotonic()
            if t - self._last_probe < PROBE_EVERY:
                return False
            self._last_probe = t
        try:
            await check()
        except Exception as e:  # noqa: BLE001 - any failure is the probe's answer
            self.note_failure(dest, e)
        else:
            self.note_ok(dest)
        return not self.is_offline()
