"""Secret storage. Production uses the macOS Keychain via `keyring`.

SQLite only ever stores the account name (a reference), never the secret.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

SERVICE = "dev.newton.agentd"


class SecretStore(Protocol):
    def get(self, account: str) -> str | None: ...
    def set(self, account: str, value: str) -> None: ...
    def delete(self, account: str) -> None: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self._data.get(account)

    def set(self, account: str, value: str) -> None:
        self._data[account] = value

    def delete(self, account: str) -> None:
        self._data.pop(account, None)


class FileSecretStore:
    """0600 JSON file. For development on machines without a keychain only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data: dict[str, str] = json.loads(self.path.read_text())
        return data

    def _save(self, data: dict[str, str]) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)

    def get(self, account: str) -> str | None:
        return self._load().get(account)

    def set(self, account: str, value: str) -> None:
        data = self._load()
        data[account] = value
        self._save(data)

    def delete(self, account: str) -> None:
        data = self._load()
        data.pop(account, None)
        self._save(data)


class KeychainSecretStore:
    def __init__(self, service: str = SERVICE) -> None:
        import keyring

        self._keyring = keyring
        self.service = service

    def get(self, account: str) -> str | None:
        value: str | None = self._keyring.get_password(self.service, account)
        return value

    def set(self, account: str, value: str) -> None:
        self._keyring.set_password(self.service, account, value)

    def delete(self, account: str) -> None:
        import keyring.errors

        try:
            self._keyring.delete_password(self.service, account)
        except keyring.errors.PasswordDeleteError:
            pass


def make_secret_store(backend: str, data_dir: Path) -> SecretStore:
    if backend == "keychain":
        return KeychainSecretStore()
    if backend == "memory":
        return MemorySecretStore()
    if backend == "file":
        return FileSecretStore(data_dir / "secrets.json")
    raise ValueError(f"unknown secret backend {backend!r}")
