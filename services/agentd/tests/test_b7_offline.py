"""B7, offline: Newton notices from its own calls that the network is gone and says so
in sentences; goals back off and look again when it's back; papers and publications
that failed for want of a network can be tried again. Every remote is mocked here
(httpx.MockTransport raising ConnectError / ConnectTimeout)."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd import network as network_mod
from newton_agentd.connectors.publish import PublishError, notion_blocks
from newton_agentd.network import NetworkState, is_offline_error
from newton_agentd.research import loop as loop_mod
from newton_agentd.storage.db import dumps, now
from test_papers import ATOM, HTML
from test_publish import TOKEN, Remote, decide, reported
from test_research_loop import feed

OFFLINE = loop_mod.OFFLINE
Handler = Callable[[httpx.Request], httpx.Response]


def factory(handler: Handler) -> Callable[[], httpx.AsyncClient]:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Down:
    """A remote that can't be reached: every request raises (ConnectError by default)."""

    def __init__(self, exc: type[httpx.TransportError] = httpx.ConnectError) -> None:
        self.exc = exc
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        raise self.exc("no route to host", request=request)


def ctx_of(client: TestClient) -> Any:
    return client.app.state.ctx  # type: ignore[attr-defined]


@pytest.fixture
def lab(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(loop_mod, "ARXIV_SPACING", 0.0)
    ctx_of(client).papers.retry_delay = 0.0
    client.patch("/profile", json={"default_model": "fake/unserved"})  # a poll needs a model
    # These tests are about arXiv and the network: the reader counts as running.
    monkeypatch.setattr(ctx_of(client).loop, "_reader_down", lambda model: None)
    return client


def goal(client: TestClient) -> str:
    r = client.post("/goals", json={"title": "Limiters", "keywords": ["flux limiter"]})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def poll_events(client: TestClient, gid: str) -> list[dict[str, Any]]:
    events = client.get(f"/events?entity_type=goal&entity_id={gid}").json()
    return [e for e in events if e["kind"] == "poll"]


# -- NetworkState ------------------------------------------------------------------------


def test_the_network_state_follows_the_last_call() -> None:
    net = NetworkState(clock=lambda: 100.0)
    assert net.snapshot() == {"state": "unknown", "since": None, "detail": None}
    request = httpx.Request("GET", "https://export.arxiv.org/api/query")
    assert net.note_failure("arxiv", httpx.ConnectError("dns", request=request)) is True
    snap = net.snapshot()
    assert net.is_offline() and snap["state"] == "offline" and snap["since"] == 100.0
    assert snap["detail"].startswith("arXiv couldn't be reached")
    net.note_ok("github")  # any answer from anywhere: online again
    assert not net.is_offline() and net.snapshot()["detail"] is None
    # An answer that was an error still reached the far end: not offline.
    assert net.note_failure("notion", PublishError("Notion answered 500")) is False
    assert net.snapshot()["state"] == "online"
    assert net.note_failure("models", httpx.ReadTimeout("slow", request=request)) is True
    assert net.is_offline()
    # A connection that broke after it was made says nothing either way: the state stays.
    assert net.note_failure("github", httpx.ReadError("reset", request=request)) is False
    assert net.is_offline()
    net.note_ok("arxiv")
    assert net.note_failure("github", httpx.RemoteProtocolError("bye", request=request)) is False
    assert net.snapshot()["state"] == "online"


def test_connection_failures_are_found_in_the_cause_chain() -> None:
    request = httpx.Request("GET", "https://api.github.com")
    try:
        try:
            raise httpx.ConnectTimeout("timed out", request=request)
        except httpx.ConnectTimeout as e:
            raise RuntimeError("publishing failed") from e
    except RuntimeError as wrapped:
        assert is_offline_error(wrapped)
    assert not is_offline_error(ValueError("bad json"))
    assert is_offline_error(OSError(errno.ENETUNREACH, "Network is unreachable"))
    assert not is_offline_error(OSError(errno.ENOENT, "No such file"))
    assert is_offline_error(ConnectionRefusedError())


def test_the_probe_runs_at_most_once_a_minute_and_only_offline() -> None:
    net = NetworkState()
    calls: list[int] = []

    async def down() -> None:
        calls.append(1)
        raise httpx.ConnectError("down")

    async def up() -> None:
        calls.append(1)

    async def run() -> None:
        assert await net.probe("arxiv", down) is True  # not offline: no probe
        net.note_failure("arxiv", httpx.ConnectError("down"))
        assert await net.probe("arxiv", down) is False
        assert await net.probe("arxiv", up) is False  # too soon: not called
        assert len(calls) == 1
        net._last_probe -= network_mod.PROBE_EVERY  # a minute later
        assert await net.probe("arxiv", up) is True
        assert net.snapshot()["state"] == "online"

    asyncio.run(run())


# -- goals: polling offline ---------------------------------------------------------------


def test_an_offline_poll_is_a_502_and_the_goal_backs_off(lab: TestClient) -> None:
    gid = goal(lab)
    down = Down()
    ctx_of(lab).papers.http_factory = factory(down)
    r = lab.post(f"/goals/{gid}/poll")
    assert r.status_code == 502
    assert r.json() == {"error": OFFLINE, "code": "offline"}
    assert len(down.urls) == 2  # retried once
    g = lab.get(f"/goals/{gid}").json()
    assert g["last_polled_at"] is None and g["last_poll_error"] == OFFLINE
    assert "poll_failures" not in g
    assert abs(g["next_poll_at"] - (time.time() + 300)) < 30
    assert ctx_of(lab).network.snapshot()["state"] == "offline"

    assert lab.post(f"/goals/{gid}/poll").status_code == 502
    g = next(x for x in lab.get("/goals").json() if x["id"] == gid)
    assert abs(g["next_poll_at"] - (time.time() + 600)) < 30  # doubling
    assert len(poll_events(lab, gid)) == 1  # one event per failure streak
    assert poll_events(lab, gid)[0]["data"]["error"] == OFFLINE

    found: list[str] = []

    def arxiv(request: httpx.Request) -> httpx.Response:
        found.append(str(request.url))
        return httpx.Response(200, content=feed([]))

    ctx_of(lab).papers.http_factory = factory(arxiv)
    summary = lab.post(f"/goals/{gid}/poll").json()
    assert summary["found"] == 0 and "error" not in summary
    g = lab.get(f"/goals/{gid}").json()
    assert g["last_poll_error"] is None and g["last_polled_at"] is not None
    assert g["next_poll_at"] == pytest.approx(g["last_polled_at"] + g["poll_hours"] * 3600)
    assert ctx_of(lab).network.snapshot()["state"] == "online"
    # A new streak gets its own event.
    ctx_of(lab).papers.http_factory = factory(Down(httpx.ConnectTimeout))
    r = lab.post(f"/goals/{gid}/poll")
    assert r.status_code == 502 and r.json()["code"] == "offline"
    assert len(poll_events(lab, gid)) == 3  # error, success, error
    assert lab.get(f"/goals/{gid}").json()["last_polled_at"] == g["last_polled_at"]


def test_the_backoff_doubles_up_to_an_hour() -> None:
    assert [loop_mod.retry_delay(n) for n in (1, 2, 3, 4, 5, 6, 50)] == [
        300, 600, 1200, 2400, 3600, 3600, 3600,
    ]  # fmt: skip


def test_arxiv_answering_badly_is_a_502_too(lab: TestClient) -> None:
    gid = goal(lab)
    calls: list[str] = []

    def busy(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503)

    ctx_of(lab).papers.http_factory = factory(busy)
    r = lab.post(f"/goals/{gid}/poll")
    assert r.status_code == 502 and r.json()["code"] == "arxiv"
    assert r.json()["error"] == "arXiv's API answered 503: Newton will look again in 5 min"
    assert len(calls) == 2  # 5xx: retried once
    assert ctx_of(lab).network.snapshot()["state"] == "online"  # arXiv did answer

    ctx_of(lab).papers.http_factory = factory(
        lambda request: httpx.Response(200, content=b"<html>Sign in to the Wi-Fi</html>")
    )
    r = lab.post(f"/goals/{gid}/poll")
    assert r.status_code == 502 and "captive portal" in r.json()["error"]


def test_background_polls_wait_while_offline_then_resume(
    lab: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ctx_of(lab)
    gid = goal(lab)
    down = Down()
    ctx.papers.http_factory = factory(down)
    ctx.network.note_failure("github", httpx.ConnectError("down"))  # learned elsewhere

    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    assert not any("search_query" in u for u in down.urls)  # no search: just the probe
    assert len(down.urls) == 1 and down.urls[0].startswith(loop_mod.ARXIV_API)
    g = lab.get(f"/goals/{gid}").json()
    assert g["last_poll_error"] == OFFLINE and g["last_polled_at"] is None
    assert len(poll_events(lab, gid)) == 1

    # Due again, still offline (the probe waits its minute): no new event, no request.
    ctx.db.execute("UPDATE goals SET next_poll_at = ? WHERE id = ?", (now() - 1, gid))
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    assert len(down.urls) == 1 and len(poll_events(lab, gid)) == 1
    g = lab.get(f"/goals/{gid}").json()
    assert g["next_poll_at"] > time.time() + 500  # second failure: 10 min

    # The network is back: the probe sees it and the waiting goal polls right away.
    monkeypatch.setattr(network_mod, "PROBE_EVERY", 0.0)
    ctx.papers.http_factory = factory(lambda request: httpx.Response(200, content=feed([])))
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    g = lab.get(f"/goals/{gid}").json()
    assert g["last_poll_error"] is None and g["last_polled_at"] is not None
    assert ctx.network.snapshot()["state"] == "online"
    assert "error" not in poll_events(lab, gid)[-1]["data"]


# -- papers -------------------------------------------------------------------------------


def test_an_unreachable_arxiv_is_a_sentence_and_retried_once(lab: TestClient) -> None:
    down = Down()
    ctx_of(lab).papers.http_factory = factory(down)
    item = lab.post("/research/ingest", json={"ref": "2401.01234"}).json()
    done = wait_for(lambda: lab.get(f"/research/items/{item['id']}").json(),
                    lambda i: i["state"] == "failed", timeout=10)  # fmt: skip
    assert done["data"]["error"] == "arXiv couldn't be reached: check the network"
    assert len(down.urls) == 2

    slow = Down(httpx.ConnectTimeout)
    ctx_of(lab).papers.http_factory = factory(slow)
    again = lab.post("/research/ingest", json={"ref": "2401.01234"}).json()
    assert again["id"] == item["id"]  # a failed item is tried again
    done = wait_for(lambda: lab.get(f"/research/items/{item['id']}").json(),
                    lambda i: i["state"] == "failed", timeout=10)  # fmt: skip
    assert done["data"]["error"] == "arXiv didn't answer within 5 s"  # the client's timeout
    assert len(slow.urls) == 2


def test_busy_answers_are_retried_once_and_not_found_is_not(lab: TestClient) -> None:
    papers = ctx_of(lab).papers
    for first in (503, 429):
        answers = [httpx.Response(first), httpx.Response(200, content=ATOM)]
        seen: list[str] = []

        def handler(request: httpx.Request, answers: list[httpx.Response] = answers,
                    seen: list[str] = seen) -> httpx.Response:  # fmt: skip
            seen.append(str(request.url))
            return answers.pop(0)

        async def get() -> httpx.Response:
            async with factory(handler)() as http:
                return await papers.fetch(http, "https://export.arxiv.org/api/query?id_list=x")

        assert lab.portal.call(get).status_code == 200  # type: ignore[union-attr]
        assert len(seen) == 2
    missing: list[str] = []

    def gone(request: httpx.Request) -> httpx.Response:
        missing.append(str(request.url))
        return httpx.Response(404)

    async def get404() -> httpx.Response:
        async with factory(gone)() as http:
            return await papers.fetch(http, "https://arxiv.org/html/2401.01234")

    assert lab.portal.call(get404).status_code == 404  # type: ignore[union-attr]
    assert len(missing) == 1


def test_a_paper_the_loop_couldnt_triage_can_be_read_again(lab: TestClient) -> None:
    ctx = ctx_of(lab)
    gid = goal(lab)
    meta = {"arxiv_id": "2609.00009", "title": "Paper on limiters", "abstract": "A scheme.",
            "authors": ["A. Author"], "published": "2026-09-30", "categories": [],
            "url": "https://arxiv.org/abs/2609.00009"}  # fmt: skip
    t = now()
    ctx.db.insert("research_items", {
        "id": "paper_untriaged", "goal_id": gid, "kind": "paper", "title": meta["title"],
        "source": "arxiv", "external_id": "2609.00009", "state": "discovered",
        "data": dumps({"paper": meta, "model": "fake/unserved", "found_by": "research_loop",
                       "error": "triage: the model service couldn't be reached"}),
        "created_at": t, "updated_at": t,
    })  # fmt: skip
    urls: list[str] = []

    def arxiv(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "/html/" in str(request.url):
            return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    ctx.papers.http_factory = factory(arxiv)
    r = lab.post("/research/ingest", json={"ref": "2609.00009"})
    assert r.status_code == 202 and r.json()["id"] == "paper_untriaged"
    assert "error" not in r.json()["data"] and r.json()["data"]["paper"] == meta
    done = wait_for(lambda: lab.get("/research/items/paper_untriaged").json(),
                    lambda i: i["state"] == "failed", timeout=30)  # no model is served  # fmt: skip
    assert done["data"]["text"]["from"] == "html"  # it was read this time, not triaged
    assert done["goal_id"] == gid and not any("export.arxiv.org" in u for u in urls)

    # A discovered item without an error is still being looked at: left alone.
    ctx.db.insert("research_items", {
        "id": "paper_busy", "goal_id": gid, "kind": "paper", "title": "t", "source": "arxiv",
        "external_id": "2609.00010", "state": "discovered", "data": dumps({"paper": meta}),
        "created_at": t, "updated_at": t,
    })  # fmt: skip
    before = len(urls)
    r = lab.post("/research/ingest", json={"ref": "2609.00010"})
    assert r.json()["id"] == "paper_busy" and r.json()["state"] == "discovered"
    assert len(urls) == before


# -- publications -------------------------------------------------------------------------


def test_an_offline_publication_says_so_and_is_sent_again_unchanged(client: TestClient) -> None:
    ctx = ctx_of(client)
    exp = reported(client)
    client.put("/connectors/github", json={"token": TOKEN})
    down = Down()
    ctx.publisher.http_factory = factory(down)
    pub = client.post(f"/experiments/{exp}/publish", json={"target": "github"}).json()
    failed = decide(client, pub)
    assert failed["state"] == "failed"
    assert failed["error"] == ("GitHub couldn't be reached: the report wasn't sent; "
                               "try again when online")  # fmt: skip
    assert ctx.network.snapshot()["state"] == "offline"
    approvals = len(client.get("/approvals").json())

    remote = Remote()
    ctx.publisher.http_factory = remote.factory()
    r = client.post(f"/publications/{pub['id']}/retry")
    assert r.status_code == 200, r.text
    assert r.json()["state"] in ("approved", "publishing", "published")
    done = wait_for(lambda: next(p for p in client.get("/publications").json()
                                 if p["id"] == pub["id"]),
                    lambda p: p["state"] in ("published", "failed"), timeout=30)  # fmt: skip
    assert done["state"] == "published" and done["error"] is None
    assert len(client.get("/approvals").json()) == approvals  # no new approval
    sent = remote.calls[0][2]["files"]["newton-report.md"]["content"]
    assert hashlib.sha256(sent.encode()).hexdigest() == pub["content_sha256"]
    assert ctx.network.snapshot()["state"] == "online"

    again = client.post(f"/publications/{pub['id']}/retry")
    assert again.status_code == 409 and again.json()["code"] == "not_failed"
    assert client.post("/publications/pub_nope/retry").status_code == 404

    # Changed after approval: not sent again without a new approval.
    ctx.publisher.http_factory = factory(Down())
    second = client.post(f"/experiments/{exp}/publish", json={"target": "github"}).json()
    assert decide(client, second)["state"] == "failed"
    frozen = Path(ctx.settings.data_dir) / "publications" / f"{second['id']}.md"
    frozen.write_text(frozen.read_text() + "\nsomething nobody approved\n")
    ctx.publisher.http_factory = remote.factory()
    r = client.post(f"/publications/{second['id']}/retry")
    assert r.status_code == 409 and r.json()["code"] == "content_changed"
    assert len(remote.calls) == 1


def test_notion_offline_and_errors_are_never_empty(client: TestClient) -> None:
    ctx = ctx_of(client)
    exp = reported(client)
    client.put("/connectors/notion", json={"token": "ntn_" + "b" * 40})
    ctx.publisher.http_factory = factory(Down(httpx.ConnectTimeout))
    pub = client.post(f"/experiments/{exp}/publish", json={
        "target": "notion", "destination": {"parent_page_id": "0" * 32}}).json()  # fmt: skip
    failed = decide(client, pub)
    assert failed["error"] == ("Notion couldn't be reached: the report wasn't sent; "
                               "try again when online")  # fmt: skip
    pages = client.get("/connectors/notion/pages")
    assert pages.status_code == 502
    assert pages.json()["error"] == "Notion couldn't be reached: check the network"
    assert ctx.publisher._failure("github", PublishError("")) == ("PublishError", "fresh")
    assert ctx.publisher._failure("github", RuntimeError("  ")) == ("RuntimeError", "fresh")


# -- regressions: polls that can't search, polls without a model, sends that may have arrived


def test_a_goal_without_keywords_backs_off_and_never_blocks_the_others(lab: TestClient) -> None:
    ctx = ctx_of(lab)
    r = lab.post("/goals", json={"title": "It is what it is"})  # keywords: []
    assert r.status_code == 201, r.text
    empty = r.json()["id"]
    valid = goal(lab)
    searched: list[str] = []

    def arxiv(request: httpx.Request) -> httpx.Response:
        searched.append(str(request.url))
        return httpx.Response(200, content=feed([]))

    ctx.papers.http_factory = factory(arxiv)
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    assert lab.get(f"/goals/{valid}").json()["last_polled_at"] is not None
    assert len(searched) == 1  # the valid goal, once: then it isn't due
    g = lab.get(f"/goals/{empty}").json()
    assert g["last_poll_error"] == loop_mod.NO_KEYWORDS and g["last_polled_at"] is None
    assert g["next_poll_at"] > time.time() + 250  # backs off: not due every pass
    assert [x["id"] for x in ctx.loop.due_goals()] == []
    assert len(poll_events(lab, empty)) == 1
    r = lab.post(f"/goals/{empty}/poll")
    assert r.status_code in (400, 422) and r.json()["error"] == loop_mod.NO_KEYWORDS


def test_an_unreadable_search_answer_is_recorded_not_a_500(lab: TestClient) -> None:
    gid = goal(lab)

    def portal(request: httpx.Request) -> httpx.Response:  # redirects to itself, forever
        return httpx.Response(302, headers={"Location": str(request.url)})

    ctx_of(lab).papers.http_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(portal), follow_redirects=True
    )
    r = lab.post(f"/goals/{gid}/poll")
    assert r.status_code == 502 and r.json()["code"] == "arxiv"
    assert r.json()["error"] == ("arXiv's answer couldn't be read (TooManyRedirects): "
                                 "Newton will look again in 5 min")  # fmt: skip
    g = lab.get(f"/goals/{gid}").json()
    assert g["last_poll_error"] == r.json()["error"] and g["next_poll_at"] > time.time()


def test_without_a_model_a_poll_is_a_failure_and_the_goal_polls_once_one_is_set(
    lab: TestClient,
) -> None:
    ctx = ctx_of(lab)
    gid = goal(lab)
    ctx.papers.http_factory = factory(Down())
    assert lab.post(f"/goals/{gid}/poll").status_code == 502  # offline: backs off
    # The network is back, the model is gone (a clean Mac without a reader).
    ctx.network.note_ok("github")
    assert lab.patch("/profile", json={"default_model": None}).status_code == 200
    ctx.db.execute("UPDATE goals SET next_poll_at = ? WHERE id = ?", (now() - 1, gid))
    for _ in range(3):
        lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    events = poll_events(lab, gid)
    assert len(events) == 2  # the offline failure, then one look without a model
    assert events[-1]["data"]["error"] == loop_mod.NO_MODEL
    g = lab.get(f"/goals/{gid}").json()
    assert g["last_poll_error"] == loop_mod.NO_MODEL  # not the stale offline sentence
    assert g["last_polled_at"] is None  # nothing was searched: no "last looked just now"
    assert time.time() + 250 < g["next_poll_at"] < time.time() + 350  # a new streak: 5 min
    assert ctx.loop.due_goals() == []

    # Poll now: the same failure, said in the answer, nothing searched.
    r = lab.post(f"/goals/{gid}/poll")
    assert r.status_code == 200 and r.json()["error"] == loop_mod.NO_MODEL
    assert lab.get(f"/goals/{gid}").json()["last_polled_at"] is None

    # Offline without a model: nothing to wait for, so not marked offline either.
    other = goal(lab)
    ctx.network.note_failure("github", httpx.ConnectError("down"))
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    g = lab.get(f"/goals/{other}").json()
    assert g["last_poll_error"] == loop_mod.NO_MODEL and g["last_polled_at"] is None

    # The user picks a model, as the error says: both look on the next pass, not in 24 h.
    ctx.network.note_ok("github")
    searched: list[str] = []

    def arxiv(request: httpx.Request) -> httpx.Response:
        searched.append(str(request.url))
        return httpx.Response(200, content=feed([]))

    ctx.papers.http_factory = factory(arxiv)
    assert lab.patch("/profile", json={"default_model": "fake/unserved"}).status_code == 200
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    assert len(searched) == 2
    for g in (lab.get(f"/goals/{gid}").json(), lab.get(f"/goals/{other}").json()):
        assert g["last_poll_error"] is None and g["last_polled_at"] is not None
        assert g["next_poll_at"] == pytest.approx(g["last_polled_at"] + g["poll_hours"] * 3600)


def test_editing_or_resuming_a_goal_clears_its_poll_error_and_backoff(lab: TestClient) -> None:
    ctx = ctx_of(lab)
    empty = lab.post("/goals", json={"title": "It is what it is"}).json()["id"]
    lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    for _ in range(3):  # backed off further and further
        ctx.db.execute("UPDATE goals SET next_poll_at = ? WHERE id = ?", (now() - 1, empty))
        lab.portal.call(ctx.loop.poll_due)  # type: ignore[union-attr]
    g = lab.get(f"/goals/{empty}").json()
    assert g["last_poll_error"] == loop_mod.NO_KEYWORDS and g["next_poll_at"] > time.time()

    r = lab.patch(f"/goals/{empty}", json={"title": "Still nothing"})  # unrelated: kept
    assert r.json()["last_poll_error"] == loop_mod.NO_KEYWORDS
    r = lab.patch(f"/goals/{empty}", json={"keywords": ["flux limiter"]})
    assert r.status_code == 200, r.text
    assert r.json()["last_poll_error"] is None and r.json()["next_poll_at"] is None
    assert [x["id"] for x in ctx.loop.due_goals()] == [empty]  # due on the next pass
    row = ctx.db.query_one("SELECT poll_failures FROM goals WHERE id = ?", (empty,))
    assert row["poll_failures"] == 0

    # Paused while failing, then resumed: the stale error and backoff go too.
    ctx.db.execute("UPDATE goals SET last_poll_error = ?, next_poll_at = ?, poll_failures = 4 "
                   "WHERE id = ?", (OFFLINE, now() + 3600, empty))  # fmt: skip
    lab.patch(f"/goals/{empty}", json={"status": "paused"})
    assert ctx.db.query_one("SELECT poll_failures FROM goals WHERE id = ?",
                            (empty,))["poll_failures"] == 4  # fmt: skip
    r = lab.patch(f"/goals/{empty}", json={"status": "active"})
    assert r.json()["last_poll_error"] is None and r.json()["next_poll_at"] is None


def _publication(client: TestClient, exp: str, target: str) -> dict[str, Any]:
    body: dict[str, Any] = {"target": target}
    if target == "notion":
        body["destination"] = {"parent_page_id": "0" * 32}
    r = client.post(f"/experiments/{exp}/publish", json=body)
    assert r.status_code in (200, 201), r.text
    return dict(r.json())


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_a_send_that_may_have_arrived_is_never_sent_twice(
    client: TestClient, exc: type[httpx.TransportError]
) -> None:
    ctx = ctx_of(client)
    exp = reported(client)
    client.put("/connectors/github", json={"token": TOKEN})
    ctx.network.note_ok("arxiv")
    ctx.publisher.http_factory = factory(Down(exc))  # the answer never comes back
    pub = _publication(client, exp, "github")
    failed = decide(client, pub)
    assert failed["state"] == "failed"
    assert failed["error"] == ("GitHub stopped answering after the report was sent: check "
                               "whether it was published before publishing it again")  # fmt: skip
    if exc is not httpx.ReadTimeout:  # a broken connection isn't a sign of being offline
        assert ctx.network.snapshot()["state"] == "online"
    remote = Remote()
    ctx.publisher.http_factory = remote.factory()
    r = client.post(f"/publications/{pub['id']}/retry")
    assert r.status_code == 409 and r.json()["code"] == "maybe_sent"
    assert remote.calls == []


def test_a_notion_page_cut_short_is_finished_on_retry_not_created_again(
    client: TestClient,
) -> None:
    ctx = ctx_of(client)
    exp = reported(client)
    client.put("/connectors/notion", json={"token": "ntn_" + "b" * 40})
    report = Path(client.get(f"/experiments/{exp}").json()["report_path"])
    report.write_text(report.read_text() + "".join(f"\n- line {i}" for i in range(250)))
    calls: list[tuple[str, str, int]] = []
    drop = {"on": True}

    def notion(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/children" in url and drop["on"]:  # the Wi-Fi drops after the page exists
            raise httpx.ConnectError("no route to host", request=request)
        calls.append((request.method, url, len(json.loads(request.content)["children"])))
        if url.endswith("/pages"):
            return httpx.Response(200, json={"id": "page-1", "url": "https://notion.so/p1"})
        return httpx.Response(200, json={})

    ctx.publisher.http_factory = factory(notion)
    pub = _publication(client, exp, "notion")
    failed = decide(client, pub)
    assert failed["state"] == "failed"
    assert failed["error"] == ("Notion couldn't be reached while adding the rest of the "
                               "report to https://notion.so/p1: try again when online to "
                               "finish that page")  # fmt: skip
    assert [c[0] for c in calls] == ["POST"] and calls[0][2] == 100

    drop["on"] = False
    r = client.post(f"/publications/{pub['id']}/retry")
    assert r.status_code == 200, r.text
    done = wait_for(lambda: next(p for p in client.get("/publications").json()
                                 if p["id"] == pub["id"]),
                    lambda p: p["state"] in ("published", "failed"), timeout=30)  # fmt: skip
    assert done["state"] == "published" and done["url"] == "https://notion.so/p1"
    assert [c[0] for c in calls].count("POST") == 1  # the same page, finished
    patched = [c for c in calls if c[0] == "PATCH"]
    assert patched and all("/blocks/page-1/children" in c[1] for c in patched)
    total = 100 + sum(c[2] for c in patched)
    frozen = Path(ctx.settings.data_dir) / "publications" / f"{pub['id']}.md"
    assert total == len(notion_blocks(frozen.read_text()))


def test_a_notion_append_that_may_have_arrived_is_not_resumed(client: TestClient) -> None:
    ctx = ctx_of(client)
    exp = reported(client)
    client.put("/connectors/notion", json={"token": "ntn_" + "b" * 40})
    report = Path(client.get(f"/experiments/{exp}").json()["report_path"])
    report.write_text(report.read_text() + "".join(f"\n- line {i}" for i in range(150)))

    def notion(request: httpx.Request) -> httpx.Response:
        if "/children" in str(request.url):
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"id": "page-1", "url": "https://notion.so/p1"})

    ctx.publisher.http_factory = factory(notion)
    pub = _publication(client, exp, "notion")
    failed = decide(client, pub)
    assert failed["error"] == ("Notion stopped answering while adding the rest of the report "
                               "to https://notion.so/p1: check that page before publishing "
                               "it again")  # fmt: skip
    r = client.post(f"/publications/{pub['id']}/retry")
    assert r.status_code == 409 and r.json()["code"] == "maybe_sent"
    assert "https://notion.so/p1" in r.json()["error"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("Notion answered 502: bad gateway", 409),  # the page may exist (blocks 101+ failed)
        ("GitHub answered 500: oops", 409),
        ("not connected to github any more", 200),  # provably nothing was sent
    ],
)
def test_a_publication_that_failed_before_b7_is_never_sent_twice(
    client: TestClient, error: str, status: int
) -> None:
    """Before B7 a failure was only publications.error, without a 'failed' event saying
    how it can be retried: assume it may have arrived, unless it surely didn't."""
    ctx = ctx_of(client)
    exp = reported(client)
    client.put("/connectors/github", json={"token": TOKEN})
    ctx.publisher.http_factory = factory(Down())
    pub = _publication(client, exp, "github")
    assert decide(client, pub)["state"] == "failed"
    ctx.db.execute("DELETE FROM events WHERE entity_type = 'publication' AND entity_id = ? "
                   "AND kind = 'failed'", (pub["id"],))  # fmt: skip
    ctx.db.execute("UPDATE publications SET error = ? WHERE id = ?", (error, pub["id"]))
    remote = Remote()
    ctx.publisher.http_factory = remote.factory()
    r = client.post(f"/publications/{pub['id']}/retry")
    assert r.status_code == status, r.text
    if status == 409:
        assert r.json()["code"] == "maybe_sent"
        assert remote.calls == []
