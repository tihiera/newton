"""Shared API-level errors."""

from __future__ import annotations

from typing import Any


class NotFound(LookupError):
    """Mapped to HTTP 404."""


class Conflict(Exception):
    """Mapped to HTTP 409. Its fields (a code, the ids it is about) join the error in the
    JSON body, so a client can act on them without parsing the sentence."""

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.fields = fields
