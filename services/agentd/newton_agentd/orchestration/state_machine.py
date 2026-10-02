"""Explicit state machines. Every transition is checked, persisted with
compare-and-set semantics, and recorded in the event log."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..storage.db import Database, dumps, now


class IllegalTransition(Exception):
    pass


class ConcurrentTransition(Exception):
    """The row was not in the expected state (someone else moved it)."""


@dataclass(frozen=True)
class StateMachine:
    name: str
    table: str
    transitions: dict[str, frozenset[str]]

    @property
    def states(self) -> frozenset[str]:
        out = set(self.transitions)
        for targets in self.transitions.values():
            out |= targets
        return frozenset(out)

    @property
    def terminal(self) -> frozenset[str]:
        return frozenset(s for s in self.states if not self.transitions.get(s))

    def check(self, src: str, dst: str) -> None:
        if dst not in self.transitions.get(src, frozenset()):
            raise IllegalTransition(f"{self.name}: {src} → {dst} is not allowed")

    def transition(
        self,
        db: Database,
        entity_id: str,
        src: str,
        dst: str,
        fields: dict[str, Any] | None = None,
        event: dict[str, Any] | None = None,
    ) -> None:
        """Move entity from src to dst atomically, updating extra fields."""
        self.check(src, dst)
        updates = {"state": dst, "updated_at": now(), **(fields or {})}
        assignments = ", ".join(f"{k} = :{k}" for k in updates)
        with db.tx():
            changed = db.execute(
                f"UPDATE {self.table} SET {assignments} WHERE id = :_id AND state = :_src",  # noqa: S608
                {**updates, "_id": entity_id, "_src": src},
            )
            if changed != 1:
                raise ConcurrentTransition(f"{self.name} {entity_id} is not in state {src}")
            record_event(
                db, self.name, entity_id, "state", {"from": src, "to": dst, **(event or {})}
            )


def record_event(
    db: Database, entity_type: str, entity_id: str, kind: str, data: dict[str, Any] | None = None
) -> None:
    db.execute(
        "INSERT INTO events (ts, entity_type, entity_id, kind, data) VALUES (?, ?, ?, ?, ?)",
        (now(), entity_type, entity_id, kind, dumps(data or {})),
    )


def _fs(*states: str) -> frozenset[str]:
    return frozenset(states)


JOB = StateMachine(
    "job",
    "jobs",
    {
        "pending_approval": _fs("queued", "rejected", "cancelled"),
        "queued": _fs("submitting", "cancelled", "failed"),
        # submitting → queued: transient infra failure, retried with backoff.
        "submitting": _fs("running", "queued", "failed", "cancelled"),
        "running": _fs("collecting", "queued", "failed", "cancelled"),
        "collecting": _fs("succeeded", "failed", "timed_out", "cancelled"),
        "succeeded": _fs(),
        "failed": _fs(),
        "timed_out": _fs(),
        "cancelled": _fs(),
        "rejected": _fs(),
    },
)

EXPERIMENT = StateMachine(
    "experiment",
    "experiments",
    {
        "awaiting_approval": _fs("executing", "rejected", "cancelled"),
        "executing": _fs("evaluating", "cancelled"),
        "evaluating": _fs("reported", "failed"),
        "reported": _fs(),
        "failed": _fs(),
        "rejected": _fs(),
        "cancelled": _fs(),
    },
)

RESEARCH_ITEM = StateMachine(
    "research_item",
    "research_items",
    {
        # B4: discovered (metadata) -> extracting (text read, model asked) -> carded.
        # B5's loop adds triage (relevance) before extraction.
        "discovered": _fs("triaged", "extracting", "failed", "dismissed"),
        "triaged": _fs("awaiting_approval", "extracting", "dismissed"),
        "awaiting_approval": _fs("extracting", "dismissed"),
        "extracting": _fs("carded", "experiment_planned", "failed", "dismissed"),
        "carded": _fs("experiment_planned", "dismissed"),
        # A plan whose experiment ended unreported (rejected, cancelled, failed) goes
        # back: to carded, or to reported when the paper was tested before.
        "experiment_planned": _fs("executing", "carded", "reported", "dismissed"),
        "executing": _fs("evaluating", "failed"),
        "evaluating": _fs("reported", "failed"),
        # A reported paper can be tested again (another baseline, or on purpose).
        "reported": _fs("experiment_planned"),
        "dismissed": _fs(),
        "failed": _fs(),
    },
)

SERVICE = StateMachine(
    "service",
    "services",
    {
        "awaiting_approval": _fs("approved", "rejected", "cancelled"),
        "approved": _fs("starting", "failed", "cancelled"),
        # starting covers the worker's starting and loading (pull, load).
        "starting": _fs("ready", "stopping", "stopped", "failed", "lost"),
        "ready": _fs("draining", "stopping", "stopped", "failed", "lost"),
        "draining": _fs("stopping", "stopped", "failed", "lost"),
        "stopping": _fs("stopped", "failed", "lost"),
        "stopped": _fs(),
        "failed": _fs(),
        "lost": _fs(),
        "rejected": _fs(),
        "cancelled": _fs(),
    },
)

ACTIVE_JOB_STATES = ("submitting", "running")
