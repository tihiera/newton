"""SQLite access: one serialized connection, file-based migrations, JSON helpers."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

Row = dict[str, Any]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._in_tx = False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[Database]:
        """Serialize a unit of work; nested calls join the outer transaction."""
        with self._lock:
            if self._in_tx:
                yield self
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                yield self
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def insert(self, table: str, row: Row) -> None:
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row)  # noqa: S608

    def query(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[Row]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- migrations ----------------------------------------------------------
    def migrate(self) -> int:
        """Apply pending migrations in order. Returns the resulting schema version."""
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        applied = {r["version"] for r in self.query("SELECT version FROM schema_migrations")}
        for version, name, sql in available_migrations():
            if version in applied:
                continue
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    for statement in split_sql(sql):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (version, name, now()),
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
        return self.schema_version()

    def schema_version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"]) if row and row["v"] is not None else 0


def available_migrations() -> list[tuple[int, str, str]]:
    out = []
    pkg = resources.files("newton_agentd.storage.migrations")
    for entry in pkg.iterdir():
        m = MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text()))
    out.sort()
    versions = [v for v, _, _ in out]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration versions")
    return out


def split_sql(sql: str) -> list[str]:
    """Split a migration file on ';' at line ends (migrations contain no triggers)."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";\n") if s.strip().rstrip(";")]
