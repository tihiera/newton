"""Approvals: the human gate in front of execution, publishing and pushes.

Each approval kind has a handler that applies the decision to its subject in
the same transaction as the decision itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..storage.db import Database, Row, dumps, loads, new_id, now
from .state_machine import record_event

Handler = Callable[[Database, Row, bool], None]


class ApprovalNotFound(KeyError):
    pass


class ApprovalAlreadyDecided(ValueError):
    pass


def approval_view(row: Row) -> dict[str, Any]:
    return {**row, "details": loads(row["details"])}


class Approvals:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def request(
        self,
        kind: str,
        subject_type: str,
        subject_id: str,
        title: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        if kind not in self._handlers:
            raise ValueError(f"no handler for approval kind {kind!r}")
        approval_id = new_id("appr")
        with self.db.tx():
            self.db.insert(
                "approvals",
                {
                    "id": approval_id,
                    "kind": kind,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "title": title,
                    "details": dumps(details or {}),
                    "status": "pending",
                    "created_at": now(),
                },
            )
            record_event(self.db, "approval", approval_id, "requested", {"kind": kind})
        return approval_id

    def get(self, approval_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            raise ApprovalNotFound(approval_id)
        return approval_view(row)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC", (status,)
            )
        else:
            rows = self.db.query("SELECT * FROM approvals ORDER BY created_at DESC")
        return [approval_view(r) for r in rows]

    def decide(self, approval_id: str, approve: bool, note: str | None = None) -> dict[str, Any]:
        with self.db.tx():
            row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                raise ApprovalNotFound(approval_id)
            if row["status"] != "pending":
                raise ApprovalAlreadyDecided(f"approval already {row['status']}")
            status = "approved" if approve else "rejected"
            self.db.execute(
                "UPDATE approvals SET status = ?, decision_note = ?, decided_at = ? WHERE id = ?",
                (status, note, now(), approval_id),
            )
            record_event(self.db, "approval", approval_id, status, {"note": note})
            self._handlers[row["kind"]](self.db, row, approve)
        return self.get(approval_id)
