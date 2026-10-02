"""B5: goals watch arXiv; papers are triaged, carded and proposed (for approval); what
experiments show becomes a finding, and a scheme already tested isn't proposed again.
Offline: arXiv is mocked, the model is the fake engine with a fixed answer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd.research import loop as loop_mod
from newton_agentd.research.papers import PaperError
from test_papers import CARD, HTML, reader
from test_services_api import stop_services


def feed(ids: list[str]) -> bytes:
    entries = "".join(
        f"""<entry><id>http://arxiv.org/abs/{aid}v1</id><published>2026-09-30T00:00:00Z</published>
        <title>Paper {aid} on limiters</title><summary>A Koren-limited scheme ({aid}).</summary>
        <author><name>A. Author</name></author><category term="physics.comp-ph"/></entry>"""
        for aid in ids
    )
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


class Arxiv:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.queries: list[str] = []

    def factory(self) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "export.arxiv.org" in url:
                self.queries.append(request.url.params.get("search_query", ""))
                return httpx.Response(200, content=feed(self.ids),
                                      headers={"content-type": "application/atom+xml"})  # fmt: skip
            if "/html/" in url:
                return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
            return httpx.Response(404)

        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def lab(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    monkeypatch.setattr(loop_mod, "ARXIV_SPACING", 0.01)
    yield client
    stop_services(client)


def goal(client: TestClient, **extra: Any) -> str:
    r = client.post("/goals", json={"title": "Better limiters for advection",
                                    "keywords": ["flux limiter", "TVD"], **extra})  # fmt: skip
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def use(client: TestClient, arxiv: Arxiv) -> None:
    client.app.state.ctx.papers.http_factory = arxiv.factory()  # type: ignore[attr-defined]


def test_new_papers_are_triaged_carded_and_proposed_once(lab: TestClient) -> None:
    arxiv = Arxiv(["2609.00001", "2609.00002"])
    use(lab, arxiv)
    reader(lab)
    gid = goal(lab)
    summary = lab.post(f"/goals/{gid}/poll").json()
    assert summary["found"] == 2 and summary["new"] == 2 and summary["relevant"] == 2
    assert summary["carded"] == 2
    assert len(summary["proposed"]) == 1  # the same scheme twice: proposed once
    assert "already planned" in summary["skipped"][0]["why"]
    assert 'abs:"flux limiter"' in arxiv.queries[0] and "cat:physics.comp-ph" in arxiv.queries[0]
    items = lab.get(f"/research/items?goal_id={gid}").json()
    assert sorted(i["state"] for i in items) == ["carded", "experiment_planned"]
    assert all(i["data"]["triage"]["relevant"] for i in items)
    exp = lab.get(f"/experiments/{summary['proposed'][0]}").json()
    assert exp["state"] == "awaiting_approval"  # nothing runs without the user
    again = lab.post(f"/goals/{gid}/poll").json()
    assert again["found"] == 2 and again["new"] == 0  # dedup by arXiv id


def test_irrelevant_papers_are_dismissed_with_the_reason(lab: TestClient) -> None:
    use(lab, Arxiv(["2609.00003"]))
    reader(lab, {"relevant": False, "why": "it is about lattice QCD"})
    gid = goal(lab)
    summary = lab.post(f"/goals/{gid}/poll").json()
    assert summary["dismissed"] == 1 and summary["proposed"] == []
    item = lab.get(f"/research/items?goal_id={gid}").json()[0]
    assert item["state"] == "dismissed" and "lattice QCD" in item["data"]["triage"]["why"]


def test_auto_propose_off_keeps_cards_only(lab: TestClient) -> None:
    use(lab, Arxiv(["2609.00004"]))
    reader(lab)
    gid = goal(lab, auto_propose=False)
    summary = lab.post(f"/goals/{gid}/poll").json()
    assert summary["carded"] == 1 and summary["proposed"] == []


def test_findings_are_remembered_and_a_tested_scheme_is_not_proposed_again(
    lab: TestClient,
) -> None:
    use(lab, Arxiv(["2609.00005"]))
    reader(lab)
    gid = goal(lab)
    first = lab.post(f"/goals/{gid}/poll").json()
    exp_id = first["proposed"][0]
    approval = next(a for a in lab.get("/approvals?status=pending").json()
                    if a["subject_id"] == exp_id)  # fmt: skip
    lab.post(f"/approvals/{approval['id']}/approve")
    wait_for(lambda: lab.get(f"/experiments/{exp_id}").json()["state"],
             lambda s: s in ("reported", "failed"), timeout=180)  # fmt: skip
    findings = lab.get(f"/findings?goal_id={gid}").json()
    assert len(findings) == 1
    f = findings[0]
    assert f["experiment_id"] == exp_id and f["evidence"] in ("green", "yellow")
    assert {c["claim"] for c in f["claims"]} >= {"order", "max_cfl", "tvd"}
    item = lab.get(f"/research/items?goal_id={gid}").json()[0]
    assert item["state"] == "reported"
    use(lab, Arxiv(["2609.00005", "2609.00006"]))  # a new paper, the same scheme
    second = lab.post(f"/goals/{gid}/poll").json()
    assert second["new"] == 1 and second["proposed"] == []
    assert "was tested in" in second["skipped"][0]["why"]


def test_without_a_model_the_poll_says_so(lab: TestClient) -> None:
    use(lab, Arxiv(["2609.00007"]))
    gid = goal(lab)
    assert "no model" in lab.post(f"/goals/{gid}/poll").json()["error"]


def test_keywords_are_words_not_query_syntax(client: TestClient) -> None:
    for bad in (['x" OR cat:hep-th'], ["a" * 80], ["(evil)"]):
        assert client.post("/goals", json={"title": "t", "keywords": bad}).status_code == 422
    assert client.post("/goals", json={"title": "t", "categories": ["math.NA) OR (x"]}
                       ).status_code == 422  # fmt: skip
    q = loop_mod.search_query(["flux limiter", "TVD"], ["math.NA", "physics.comp-ph"])
    assert q == '(abs:"flux limiter" OR abs:TVD) AND (cat:math.NA OR cat:physics.comp-ph)'
    with pytest.raises(PaperError):
        loop_mod.search_query([], ["math.NA"])
    assert json.dumps(CARD)  # (the fake reader answers with this card)
