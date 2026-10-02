"""Keywords a research goal searches for, proposed from its title and description: the
reader model's when it answers, else the text's own terms; always checked."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from newton_agentd.research import keywords


def test_the_texts_own_terms() -> None:
    topic = ("Investigate 2nd-order temporal convergence for pressure in OpenFOAM-style solvers. "
             "BDF2 with time-inconsistent Rhie–Chow stabilization")  # fmt: skip
    found = keywords.from_text("Second order pressure convergence", topic)
    assert found[:3] == ["second order", "order pressure", "pressure convergence"]
    assert {"OpenFOAM", "BDF2", "Rhie-Chow"} <= set(found)  # names, en dash as a hyphen
    assert "2nd-order" not in found and len(found) <= keywords.MAX_KEYWORDS
    assert keywords.from_text("Higher-order advection schemes that beat upwind", "") == [
        "higher-order advection schemes", "upwind"]  # fmt: skip
    assert keywords.from_text("It is what it is", "") == []


def test_only_valid_keywords_get_through() -> None:
    raw = ["flux limiter", 'x"; rm', "TVD", "tvd", "a", 3, "MUSCL – minmod", "w" * 61]
    assert keywords.clean(raw) == ["flux limiter", "TVD", "MUSCL - minmod"]
    assert keywords.clean("not a list") == []


class FakeRouter:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.asked: list[dict[str, Any]] = []

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(payload)
        if self.content is None:
            raise RuntimeError("no service serves this model")
        return {"body": {"choices": [{"message": {"content": self.content}}]}, "provenance": {}}


async def test_the_reader_proposes_and_the_text_is_the_fallback() -> None:
    router = FakeRouter('{"keywords": ["BDF2", "pressure convergence", "Rhie-Chow"]}')
    found, source = await keywords.suggest(router, "llama3.2:3b", "Pressure", "BDF2 on meshes")
    assert (found, source) == (["BDF2", "pressure convergence", "Rhie-Chow"], "model")
    assert router.asked[0]["model"] == "llama3.2:3b"
    for broken in (None, "not json", '{"keywords": ["one"]}'):  # down, garbage, too few
        found, source = await keywords.suggest(FakeRouter(broken), "m", "Flux limiters", "")
        assert (found, source) == (["flux limiters"], "text")
    assert await keywords.suggest(FakeRouter("{}"), None, "Flux limiters", "") == (
        ["flux limiters"], "text")  # fmt: skip


def test_a_goal_without_keywords_gets_them_from_its_topic(client: TestClient) -> None:
    r = client.post("/goals", json={"title": "Higher-order advection schemes that beat upwind"})
    assert r.status_code == 201, r.text
    goal = r.json()
    assert goal["keywords"] == ["higher-order advection schemes", "upwind"]
    events = client.get(f"/events?entity_id={goal['id']}").json()
    assert any(e["kind"] == "keywords" and e["data"]["source"] == "text" for e in events)
    given = client.post("/goals", json={"title": "T", "keywords": ["flux limiter"]}).json()
    assert given["keywords"] == ["flux limiter"]  # the user's own are kept as they are


def test_suggest_keywords_for_the_dialog(client: TestClient) -> None:
    topic = {"title": "Flux limiters for TVD schemes", "description": "MUSCL with van Leer"}
    r = client.post("/goals/suggest-keywords", json=topic)
    assert r.status_code == 200, r.text
    assert r.json() == {"keywords": ["flux limiters", "tvd schemes", "TVD", "MUSCL"],
                        "source": "text"}  # fmt: skip
