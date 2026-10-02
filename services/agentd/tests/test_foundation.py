"""B0: health, auth, migrations, persistence, state machines."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.orchestration.state_machine import (
    EXPERIMENT,
    JOB,
    RESEARCH_ITEM,
    ConcurrentTransition,
    IllegalTransition,
)
from newton_agentd.storage.db import Database, available_migrations


def test_health_is_public_and_reports_schema(settings: Settings) -> None:
    with TestClient(create_app(settings)) as c:
        body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["db"]["schema_version"] == max(v for v, _, _ in available_migrations())


def test_api_requires_token(settings: Settings) -> None:
    with TestClient(create_app(settings)) as c:
        assert c.get("/hosts").status_code == 401
        assert c.get("/hosts", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = c.get("/hosts", headers={"Authorization": f"Bearer {settings.api_token}"})
        assert ok.status_code == 200


def test_rejects_foreign_host_header(client: TestClient) -> None:
    r = client.get("/health", headers={"Host": "evil.example:8765"})
    assert r.status_code == 403


def test_token_file_created_with_0600(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path / "d", secret_backend="memory", api_token=None)
    s.ensure_dirs()
    token = s.resolve_api_token()
    path = tmp_path / "d" / "api-token"
    assert path.read_text() == token
    assert path.stat().st_mode & 0o777 == 0o600
    assert Settings(data_dir=tmp_path / "d", api_token=None).resolve_api_token() == token


def test_state_survives_restart(settings: Settings) -> None:
    with TestClient(create_app(settings), headers={"Authorization": "Bearer test-api-token"}) as c:
        goal = c.post("/goals", json={"title": "Beat upwind", "keywords": ["advection"]}).json()
    with TestClient(create_app(settings), headers={"Authorization": "Bearer test-api-token"}) as c:
        again = c.get(f"/goals/{goal['id']}").json()
        hosts = c.get("/hosts").json()
    assert again["title"] == "Beat upwind"
    assert again["keywords"] == ["advection"]
    assert [h["id"] for h in hosts] == ["local"]


def test_migrations_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    v1 = db.migrate()
    v2 = db.migrate()
    assert v1 == v2 >= 1
    assert len(db.query("SELECT * FROM schema_migrations")) == v1


def test_goal_crud_and_events(client: TestClient) -> None:
    goal = client.post("/goals", json={"title": "g"}).json()
    updated = client.patch(f"/goals/{goal['id']}", json={"status": "paused"}).json()
    assert updated["status"] == "paused"
    assert client.get("/goals/missing").status_code == 404
    kinds = [e["kind"] for e in client.get(f"/events?entity_id={goal['id']}").json()]
    assert kinds == ["created", "updated"]


def test_state_machines_reject_illegal_transitions() -> None:
    JOB.check("queued", "submitting")
    with pytest.raises(IllegalTransition):
        JOB.check("queued", "succeeded")
    with pytest.raises(IllegalTransition):
        JOB.check("succeeded", "running")
    with pytest.raises(IllegalTransition):
        EXPERIMENT.check("awaiting_approval", "reported")
    assert RESEARCH_ITEM.terminal == {"reported", "dismissed", "failed"}
    path = ["discovered", "triaged", "awaiting_approval", "extracting",
            "experiment_planned", "executing", "evaluating", "reported"]  # fmt: skip
    for a, b in zip(path, path[1:], strict=False):
        RESEARCH_ITEM.check(a, b)


def test_transition_is_compare_and_set(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    db.migrate()
    db.execute(
        "INSERT INTO hosts (id, name, kind, token_ref, created_at, updated_at) "
        "VALUES ('h', 'h', 'local', 'x', 0, 0)"
    )
    db.execute(
        "INSERT INTO jobs (id, host_id, role, label, manifest, state, created_at, updated_at) "
        "VALUES ('j', 'h', 'r', 'l', '{}', 'queued', 0, 0)"
    )
    JOB.transition(db, "j", "queued", "submitting")
    with pytest.raises(ConcurrentTransition):
        JOB.transition(db, "j", "queued", "submitting")
    events = db.query("SELECT * FROM events WHERE entity_id = 'j'")
    assert len(events) == 1
