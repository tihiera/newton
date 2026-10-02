"""A poll reads papers only with a reader that runs: otherwise it stops with a sentence
(no papers started that can't be read), and papers left unread while no reader ran are
read on the next poll that has one."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from newton_agentd.research import loop as loop_mod
from newton_agentd.storage.db import dumps, now
from test_research_loop import Arxiv


def ctx_of(client: TestClient) -> Any:
    return client.app.state.ctx  # type: ignore[attr-defined]


def test_no_running_reader_stops_the_poll_with_a_sentence(client: TestClient) -> None:
    ctx = ctx_of(client)
    arxiv = Arxiv(["2609.00001"])
    ctx.papers.http_factory = arxiv.factory()
    client.patch("/profile", json={"default_model": "nemotron-mini:4b"})  # nothing serves it
    gid = client.post("/goals", json={"title": "Limiters", "keywords": ["flux limiter"]}).json()[
        "id"
    ]
    summary = client.post(f"/goals/{gid}/poll").json()
    assert summary["error"].startswith(loop_mod.READER_DOWN)
    assert "nemotron-mini:4b" in summary["error"] and "start it in Models" in summary["error"]
    assert arxiv.queries == []  # nothing searched, no paper started
    assert client.get("/research/items").json() == []
    goal = client.get(f"/goals/{gid}").json()
    assert goal["last_poll_error"] == summary["error"] and goal["last_polled_at"] is None
    assert loop_mod.failure_code(goal["last_poll_error"]) == "model"


def test_papers_left_unread_are_read_on_the_next_poll(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ctx_of(client)
    ctx.papers.http_factory = Arxiv([]).factory()
    client.patch("/profile", json={"default_model": "fake/reader"})
    monkeypatch.setattr(ctx.loop, "_reader_down", lambda model: None)
    gid = client.post("/goals", json={"title": "Limiters", "keywords": ["flux limiter"]}).json()[
        "id"
    ]
    paper = {"arxiv_id": "2609.00002", "title": "Stuck", "abstract": "A limiter.", "authors": [],
             "published": "2026-09-30", "categories": [], "url": None}  # fmt: skip
    ctx.db.insert("research_items", {
        "id": "paper-stuck", "goal_id": gid, "kind": "paper", "title": "Stuck",
        "source": "arxiv", "external_id": "2609.00002", "state": "discovered",
        "data": dumps({"paper": paper, "error": "triage: no model service runs 'x'"}),
        "created_at": now(), "updated_at": now(),
    })  # fmt: skip
    read: list[str] = []

    async def triage(goal: Any, p: dict[str, Any], model: str) -> tuple[bool, str]:
        read.append(p["arxiv_id"])
        return False, "not about advection"

    monkeypatch.setattr(ctx.loop, "triage", triage)
    summary = client.post(f"/goals/{gid}/poll").json()
    assert summary["retried"] == 1 and read == ["2609.00002"]
    item = client.get("/research/items/paper-stuck").json()
    assert item["state"] == "dismissed" and item["data"].get("error") is None


def test_papers_the_reader_answered_badly_are_read_again_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ctx_of(client)
    ctx.papers.http_factory = Arxiv([]).factory()
    client.patch("/profile", json={"default_model": "fake/reader"})
    monkeypatch.setattr(ctx.loop, "_reader_down", lambda model: None)
    limiters = {"title": "Limiters", "keywords": ["flux limiter"]}
    gid = client.post("/goals", json=limiters).json()["id"]
    paper = {"arxiv_id": "2609.00003", "title": "Badly read", "abstract": "A limiter.",
             "authors": [], "published": "2026-09-30", "categories": [], "url": None}  # fmt: skip
    ctx.db.insert("research_items", {
        "id": "paper-bad", "goal_id": gid, "kind": "paper", "title": "Badly read",
        "source": "arxiv", "external_id": "2609.00003", "state": "failed",
        "data": dumps({"paper": paper, "error": "the model's answer is not valid JSON"}),
        "created_at": now(), "updated_at": now(),
    })  # fmt: skip
    read: list[str] = []

    async def triage(goal: Any, p: dict[str, Any], model: str) -> tuple[bool, str]:
        read.append(p["arxiv_id"])
        return False, "not about advection"

    monkeypatch.setattr(ctx.loop, "triage", triage)
    client.post(f"/goals/{gid}/poll")
    assert read == ["2609.00003"]
    item = client.get("/research/items/paper-bad").json()
    assert item["state"] == "dismissed" and item["data"]["reread"] == 1
    client.post(f"/goals/{gid}/poll")
    assert read == ["2609.00003"]  # once: a paper that fails again stays as it is
