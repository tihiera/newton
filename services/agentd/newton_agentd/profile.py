"""The local profile: no user accounts, one person on one Mac.

It holds the few things that are personal rather than per-host: a display name, the
model `"default"` stands for in the router, and the router key. The key lets tools
that only need inference (scripts, editors, paper extraction) use `/v1/*` without the
admin token that can approve jobs or delete hosts. It lives in the secret store (the
Keychain on macOS); the database never sees it.
"""

from __future__ import annotations

import secrets as token_source
from typing import Any

from .secrets import SecretStore
from .storage.db import Database, now

ROUTER_KEY_REF = "router-key"


class Profile:
    def __init__(self, db: Database, secrets: SecretStore) -> None:
        self.db = db
        self.secrets = secrets
        self._key: str | None = None  # checked on every /v1 request: not a Keychain read each time

    def get(self) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM profile WHERE id = 1")
        assert row is not None  # created by the migration
        return {
            "display_name": row["display_name"],
            "default_model": row["default_model"],
            "mac_models": bool(row["mac_models"]),
            "updated_at": row["updated_at"],
        }

    def mac_models(self) -> bool:
        row = self.db.query_one("SELECT mac_models FROM profile WHERE id = 1")
        return bool(row and row["mac_models"])

    def update(self, **fields: Any) -> dict[str, Any]:
        if fields.get("mac_models") is None:
            fields.pop("mac_models", None)  # null: unchanged
        else:
            fields["mac_models"] = int(fields["mac_models"])
        if fields:
            assignments = ", ".join(f"{k} = :{k}" for k in fields)
            self.db.execute(
                f"UPDATE profile SET {assignments}, updated_at = :_t WHERE id = 1",  # noqa: S608
                {**fields, "_t": now()},
            )
        return self.get()

    def cached_router_key(self) -> str | None:
        """What the request path may use: never a secret-store read (it can block, or
        prompt, while the keychain is locked)."""
        return self._key

    def router_key(self) -> str:
        """The key, created on first use."""
        if self._key:
            return self._key
        key = self.secrets.get(ROUTER_KEY_REF)
        if not key:
            key = token_source.token_urlsafe(32)
            self.secrets.set(ROUTER_KEY_REF, key)
        self._key = key
        return key

    def rotate_router_key(self) -> str:
        key = token_source.token_urlsafe(32)
        self.secrets.set(ROUTER_KEY_REF, key)
        self._key = key
        return key
