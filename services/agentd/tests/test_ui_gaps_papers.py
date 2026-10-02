"""What the UI needs from papers: arXiv's journal reference and DOI; proposing again
(a reported paper, a retest) with scientific memory's answer as a code; the paper cut to
fit the reader's context, and a clear error when it didn't fit. Offline: arXiv is
mocked, the model is the fake engine or a fake router."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd.research import papers, schemes
from newton_agentd.storage.db import dumps, new_id, now
from test_papers import ATOM, CARD, ingest
from test_services_api import FAKE, stop_services

ARXIV_ENTRY = """<entry xmlns="http://www.w3.org/2005/Atom"
    xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/abs/2401.01234v1</id>
  <published>2024-01-02T00:00:00Z</published>
  <title>A Koren-limited scheme</title><summary>We propose a scheme.</summary>
  <author><name>A. Author</name></author>%s
</entry>"""


def entry(extra: str = "") -> dict[str, Any]:
    return papers.parse_entry(ET.fromstring(ARXIV_ENTRY % extra), "2401.01234")  # noqa: S314


def test_journal_reference_and_doi() -> None:
    meta = entry("""<arxiv:journal_ref>J. Comput. Phys.  412
        (2024) 109432</arxiv:journal_ref><arxiv:doi>10.1016/j.jcp.2024.109432</arxiv:doi>""")
    assert meta["journal_ref"] == "J. Comput. Phys. 412 (2024) 109432"
    assert meta["doi"] == "10.1016/j.jcp.2024.109432"


def test_no_journal_reference_nor_doi() -> None:
    meta = entry()
    assert meta["journal_ref"] is None and meta["doi"] is None
    assert entry("<arxiv:doi>  </arxiv:doi>")["doi"] is None
    feed = papers.parse_atom(ATOM, "2401.01234")  # test_papers' feed: no arxiv elements
    assert feed["journal_ref"] is None and feed["doi"] is None


# -- proposing: a card, a reported paper, a retest ----------------------------------------


def carded(client: TestClient, aid: str, state: str = "carded") -> str:
    """A research item as the reader leaves it (the card's scheme, checked)."""
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    doc, note = papers.scheme_from_card(CARD, papers.slugify(CARD["method"]["name"], aid))
    assert doc is not None, note
    checked = schemes.check(ctx.settings.benchmarks_dir, doc)
    item_id, t = new_id("paper"), now()
    ctx.db.insert("research_items", {
        "id": item_id, "goal_id": None, "kind": "paper", "title": f"Paper {aid}",
        "source": "arxiv", "external_id": aid, "state": state, "created_at": t,
        "updated_at": t, "data": dumps({
            "paper": {"title": f"Paper {aid}"}, "card": CARD, "scheme_note": note,
            "scheme_ir": checked["document"], "scheme_ir_digest": checked["digest"],
            "method_digest": checked["method_digest"],
        }),
    })  # fmt: skip
    return item_id


def propose(client: TestClient, item_id: str, **extra: Any) -> httpx.Response:
    return client.post(f"/research/items/{item_id}/propose",
                       json={"host_id": "local", "backend": "cpu", **extra})  # fmt: skip


def report(client: TestClient, exp_id: str, evidence: str = "green") -> None:
    """The experiment ran and was evaluated (stands in for a run: the run itself has
    its own tests)."""
    evaluation = {
        "verdicts": [{"evidence": evidence, "summary": f"Koren holds ({exp_id})"}],
        "variants": [{"role": "candidate", "assumptions": [
            {"claim": "order", "claimed": 2, "holds": True},
            {"claim": "tvd", "claimed": True, "holds": True},
        ]}],
    }  # fmt: skip
    client.app.state.ctx.db.execute(  # type: ignore[attr-defined]
        "UPDATE experiments SET state = 'reported', evaluation = ?, evidence = ? WHERE id = ?",
        (dumps(evaluation), evidence, exp_id),
    )


def state(client: TestClient, item_id: str) -> str:
    return str(client.get(f"/research/items/{item_id}").json()["state"])


def test_a_card_proposes(client: TestClient) -> None:
    item_id = carded(client, "2401.00001")
    r = propose(client, item_id)
    assert r.status_code == 201, r.text
    assert r.json()["research_item_id"] == item_id and r.json()["state"] == "awaiting_approval"
    assert state(client, item_id) == "experiment_planned"
    events = client.get(f"/events?entity_id={item_id}").json()
    assert any(e["kind"] == "proposed" and e["data"]["experiment_id"] == r.json()["id"]
               for e in events)  # fmt: skip


def test_unknown_or_unready_items_dont_propose(client: TestClient) -> None:
    r = propose(client, "paper_nope")
    assert r.status_code == 404 and r.json()["error"] == "no research item paper_nope"
    item_id = carded(client, "2401.00002", state="extracting")
    r = propose(client, item_id)
    assert r.status_code == 400 and "only a carded paper" in r.json()["error"]


def test_the_same_scheme_already_planned_is_a_409_with_its_item(client: TestClient) -> None:
    first, second = carded(client, "2401.00003"), carded(client, "2401.00004")
    assert propose(client, first).status_code == 201
    r = propose(client, second)
    assert r.status_code == 409
    assert r.json() == {"error": f"the same scheme is already planned (from {first})",
                        "code": "already_planned", "research_item_id": first}  # fmt: skip
    assert state(client, second) == "carded"  # nothing changed
    r = propose(client, second, retest=True)
    assert r.status_code == 201, r.text
    assert state(client, second) == "experiment_planned"


def test_a_run_that_showed_nothing_doesnt_block_a_new_one(client: TestClient) -> None:
    # e.g. the baseline didn't run: reported, but with "unknown" evidence.
    item_id = carded(client, "2401.00009")
    first = propose(client, item_id).json()["id"]
    report(client, first, evidence="unknown")
    assert [f["evidence"] for f in client.get("/findings").json()] == ["unknown"]
    assert state(client, item_id) == "reported"
    loop = client.app.state.ctx.loop  # type: ignore[attr-defined]
    assert loop.already_tested(papers_digest(client, item_id), "another-item") is None
    r = propose(client, item_id)
    assert r.status_code == 201, r.text


def papers_digest(client: TestClient, item_id: str) -> str:
    digest: str = client.get(f"/research/items/{item_id}").json()["data"]["method_digest"]
    return digest


def test_a_reported_paper_proposes_again_and_its_finding_counts(client: TestClient) -> None:
    item_id = carded(client, "2401.00005")
    first = propose(client, item_id).json()["id"]
    report(client, first)
    findings = client.get("/findings").json()
    assert [f["experiment_id"] for f in findings] == [first]
    assert state(client, item_id) == "reported"

    # Its own finding is scientific memory too: the same scheme again needs retest.
    r = propose(client, item_id, baseline="lax_wendroff")
    assert r.status_code == 409
    assert r.json() == {"error": f"the same scheme was tested in {first} (green)",
                        "code": "already_tested", "experiment_id": first}  # fmt: skip
    assert state(client, item_id) == "reported"
    other = carded(client, "2401.00006")  # another paper, the same method
    assert propose(client, other).json()["code"] == "already_tested"

    r = propose(client, item_id, baseline="lax_wendroff", retest=True)
    assert r.status_code == 201, r.text
    second = r.json()["id"]
    assert second != first and r.json()["research_item_id"] == item_id
    assert "lax_wendroff" in r.json()["title"]
    assert state(client, item_id) == "experiment_planned"
    proposed = [e for e in client.get(f"/events?entity_id={item_id}").json()
                if e["kind"] == "proposed"]  # fmt: skip
    assert [e["data"]["retest"] for e in proposed] == [False, True]

    # The second experiment reports: a new finding, and the item is reported again.
    report(client, second, evidence="yellow")
    mine = sorted((f["experiment_id"], f["evidence"]) for f in client.get("/findings").json()
                  if f["research_item_id"] == item_id)  # fmt: skip
    assert mine == sorted([(first, "green"), (second, "yellow")])
    assert state(client, item_id) == "reported"
    moves = [(e["data"]["from"], e["data"]["to"])
             for e in client.get(f"/events?entity_id={item_id}").json()
             if e["kind"] == "state"]  # fmt: skip
    again = [("reported", "experiment_planned"), ("experiment_planned", "executing"),
             ("executing", "evaluating"), ("evaluating", "reported")]  # fmt: skip
    assert moves[-4:] == again
    # The newest finding is what memory answers with now.
    r = propose(client, item_id)
    assert r.status_code == 409 and r.json()["experiment_id"] == second


def reject(client: TestClient, exp_id: str) -> None:
    approval = next(a for a in client.get("/approvals?status=pending").json()
                    if a["subject_id"] == exp_id)  # fmt: skip
    r = client.post(f"/approvals/{approval['id']}/reject", json={"note": "not now"})
    assert r.status_code == 200, r.text
    assert client.get(f"/experiments/{exp_id}").json()["state"] == "rejected"


def test_a_rejected_retest_leaves_the_paper_reported(client: TestClient) -> None:
    item_id = carded(client, "2401.00011")
    first = propose(client, item_id).json()["id"]
    report(client, first)
    client.get("/findings")
    second = propose(client, item_id, retest=True).json()["id"]
    assert state(client, item_id) == "experiment_planned"
    reject(client, second)

    # Another paper with the same method: the dead plan isn't "already planned".
    other = carded(client, "2401.00012")
    r = propose(client, other)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "already_tested" and r.json()["experiment_id"] == first
    assert state(client, item_id) == "reported"  # its finding still stands
    moves = [(e["data"]["from"], e["data"]["to"], e["data"].get("experiment_id"))
             for e in client.get(f"/events?entity_id={item_id}").json()
             if e["kind"] == "state"]  # fmt: skip
    assert moves[-1] == ("experiment_planned", "reported", second)

    # It proposes again: memory answers, and a retest goes through.
    assert propose(client, item_id).json()["code"] == "already_tested"
    r = propose(client, item_id, retest=True)
    assert r.status_code == 201, r.text
    assert state(client, item_id) == "experiment_planned"


def test_a_cancelled_or_failed_plan_returns_the_card(client: TestClient) -> None:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    item_id = carded(client, "2401.00013")
    exp_id = propose(client, item_id).json()["id"]
    assert client.post(f"/experiments/{exp_id}/cancel").status_code == 200
    assert client.get(f"/experiments/{exp_id}").json()["state"] == "cancelled"
    other = carded(client, "2401.00014")  # the same method: nothing blocks it now
    r = propose(client, other)
    assert r.status_code == 201, r.text
    assert state(client, item_id) == "carded"

    ctx.db.execute("UPDATE experiments SET state = 'failed' WHERE id = ?", (r.json()["id"],))
    assert ctx.loop.record_findings() == 0  # no finding, but the plan is over
    assert state(client, other) == "carded"
    assert ctx.db.query("SELECT * FROM findings") == []
    r = propose(client, item_id)
    assert r.status_code == 201, r.text  # the card proposes again
    assert state(client, item_id) == "experiment_planned"


def test_a_plan_still_waiting_stays_planned(client: TestClient) -> None:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    item_id = carded(client, "2401.00015")
    first = propose(client, item_id).json()["id"]
    reject(client, first)
    second = propose(client, item_id).json()["id"]  # the newest experiment is what counts
    assert ctx.loop.settle_plans() == 0
    assert state(client, item_id) == "experiment_planned"
    assert client.get(f"/experiments/{second}").json()["state"] == "awaiting_approval"


def test_proposing_reads_memory_as_of_now(client: TestClient) -> None:
    """An experiment that reported a moment ago counts, before the loop's next pass."""
    item_id = carded(client, "2401.00016")
    first = propose(client, item_id).json()["id"]
    report(client, first)  # no GET /findings, no loop pass: the finding isn't recorded yet
    other = carded(client, "2401.00017")
    r = propose(client, other)
    assert r.status_code == 409
    assert r.json() == {"error": f"the same scheme was tested in {first} (green)",
                        "code": "already_tested", "experiment_id": first}  # fmt: skip
    r = propose(client, item_id)  # the paper itself is reported now, not still planned
    assert r.status_code == 409 and r.json()["code"] == "already_tested"
    assert state(client, item_id) == "reported"


def test_the_loop_still_reads_memory_as_a_sentence(client: TestClient) -> None:
    loop = client.app.state.ctx.loop  # type: ignore[attr-defined]
    item_id = carded(client, "2401.00007")
    digest = client.get(f"/research/items/{item_id}").json()["data"]["method_digest"]
    assert loop.already_tested(digest, item_id) is None
    propose(client, item_id)
    other = carded(client, "2401.00008")
    planned = f"the same scheme is already planned (from {item_id})"
    assert loop.already_tested(digest, other) == planned
    assert loop.already_tested(digest, item_id) is None  # its own plan isn't a duplicate


# -- the reader's context ----------------------------------------------------------------


class FakeRouter:
    def __init__(self, context: int | None, reply: str = json.dumps(CARD),
                 finish: str = "stop", usage: dict[str, Any] | None = None) -> None:  # fmt: skip
        self.context = context
        self.reply = reply
        self.finish = finish
        self.usage = usage or {}
        self.payloads: list[dict[str, Any]] = []
        self.asked: list[str] = []

    def context_length(self, model: str) -> int | None:
        self.asked.append(model)
        return self.context

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "body": {"choices": [{"message": {"content": self.reply},
                                  "finish_reason": self.finish}], "usage": self.usage},
            "provenance": {"service": "svc_fake", "model": payload["model"]},
        }  # fmt: skip


META = {"title": "A Koren-limited scheme", "abstract": "We propose a scheme. " * 20}
TEXT = "".join(f"Paragraph {i}: the scheme $u_t + a u_x = 0$ is TVD.\n" for i in range(800))


def reader_with(client: TestClient, router: FakeRouter) -> papers.Papers:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    return papers.Papers(ctx.settings, ctx.db, router, ctx.profile)


def sent(router: FakeRouter) -> tuple[str, str]:
    messages = router.payloads[-1]["messages"]
    return messages[0]["content"], messages[1]["content"]


async def test_no_known_context_reads_as_before(client: TestClient) -> None:
    router = FakeRouter(None)
    card, prov = await reader_with(client, router).extract(META, TEXT, "fake/reader")
    assert card["method"]["limiter"] == "koren" and router.asked == ["fake/reader"]
    assert len(TEXT) > papers.TEXT_FOR_MODEL
    assert prov["characters_read"] == papers.TEXT_FOR_MODEL
    assert sent(router)[1].endswith(TEXT[: papers.TEXT_FOR_MODEL])
    assert "context_length" not in prov
    assert router.payloads[-1]["max_tokens"] == papers.CARD_TOKENS


async def test_the_paper_is_cut_to_fit_a_small_context(client: TestClient) -> None:
    router = FakeRouter(4096)
    card, prov = await reader_with(client, router).extract(META, TEXT, "fake/reader")
    system, user = sent(router)
    read = prov["characters_read"]
    assert 1000 < read < papers.TEXT_FOR_MODEL and user.endswith(TEXT[:read])
    assert TEXT[: read + 1] not in user
    estimated = (len(system) + len(user)) / papers.CHARS_PER_TOKEN + papers.CARD_TOKENS
    assert estimated <= 4096 - papers.CONTEXT_MARGIN
    assert prov["context_length"] == 4096 and prov["service"] == "svc_fake"


async def test_a_large_context_never_reads_more_than_before(client: TestClient) -> None:
    router = FakeRouter(131072)
    _, prov = await reader_with(client, router).extract(META, TEXT, "fake/reader")
    assert prov["characters_read"] == papers.TEXT_FOR_MODEL
    assert prov["context_length"] == 131072


async def test_a_short_paper_is_read_whole(client: TestClient) -> None:
    _, prov = await reader_with(client, FakeRouter(4096)).extract(META, TEXT[:3000], "m")
    assert prov["characters_read"] == 3000


async def test_a_context_too_short_for_any_paper_says_so(client: TestClient) -> None:
    router = FakeRouter(2048)
    with pytest.raises(papers.PaperError) as e:
        await reader_with(client, router).extract(META, TEXT, "fake/reader")
    assert str(e.value) == ("the model's context (2048 tokens) is too short to read a paper: "
                            "give the reader service a longer context_length")  # fmt: skip
    assert router.payloads == []  # the model wasn't asked


async def test_a_card_cut_by_a_full_context_says_so(client: TestClient) -> None:
    cut = json.dumps(CARD)[:300]
    router = FakeRouter(4096, cut, usage={"prompt_tokens": 3500, "completion_tokens": 590})
    with pytest.raises(papers.PaperError) as e:
        await reader_with(client, router).extract(META, TEXT, "fake/reader")
    assert str(e.value) == ("the paper didn't fit the model's context (4096 tokens): give "
                            "the reader service a longer context_length")  # fmt: skip


async def test_a_card_cut_at_the_length_limit_says_so(client: TestClient) -> None:
    cut = json.dumps(CARD)[:300]
    router = FakeRouter(None, cut, finish="length",
                        usage={"prompt_tokens": 6000, "completion_tokens": 1200})  # fmt: skip
    with pytest.raises(papers.PaperError) as e:
        await reader_with(client, router).extract(META, TEXT, "fake/reader")
    assert "didn't fit the model's context (7200 tokens)" in str(e.value)
    router = FakeRouter(None, cut, finish="length")
    with pytest.raises(papers.PaperError) as e:
        await reader_with(client, router).extract(META, TEXT, "fake/reader")
    assert str(e.value) == ("the paper didn't fit the model's context: give the reader "
                            "service a longer context_length")  # fmt: skip


async def test_bad_json_with_room_left_is_still_bad_json(client: TestClient) -> None:
    router = FakeRouter(4096, '{"relevant": tru}', usage={"prompt_tokens": 2000,
                                                          "completion_tokens": 10})  # fmt: skip
    with pytest.raises(papers.PaperError, match="not valid JSON"):
        await reader_with(client, router).extract(META, TEXT, "fake/reader")


def test_the_budget() -> None:
    assert papers.text_budget(None, 5000) == papers.TEXT_FOR_MODEL
    assert papers.text_budget(0, 5000) == papers.TEXT_FOR_MODEL
    assert papers.text_budget(8192, 2000) == (8192 - 1200 - 256) * 3 - 2000
    assert papers.text_budget(262144, 2000) == papers.TEXT_FOR_MODEL
    assert papers.text_budget(1024, 2000) == 0


# -- end to end: the router knows the reader's context ---------------------------------------

LONG_HTML = ("<html><body><h1>Introduction</h1>"
             + "<p>The scheme is TVD for CFL up to one, and second order. " * 600
             + "</p></body></html>").encode()  # fmt: skip


@pytest.fixture
def lab(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "export.arxiv.org" in url:
            return httpx.Response(200, content=ATOM,
                                  headers={"content-type": "application/atom+xml"})  # fmt: skip
        if "/html/" in url:
            return httpx.Response(200, content=LONG_HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    client.app.state.ctx.papers.http_factory = (  # type: ignore[attr-defined]
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    yield client
    stop_services(client)


def reader(client: TestClient, name: str, context_length: int) -> str:
    r = client.post("/services", json={"host_id": "local", "name": name, "settings": {
        **FAKE, "model": "fake/reader", "context_length": context_length,
        "fake": {"reply_text": json.dumps(CARD)}}})  # fmt: skip
    assert r.status_code in (200, 201, 202), r.text
    wait_for(lambda: client.get(f"/services/{r.json()['id']}").json(),
             lambda s: (s.get("endpoint") or {}).get("reachable"), timeout=60)  # fmt: skip
    return str(r.json()["id"])


def test_the_reader_service_context_cuts_the_paper(lab: TestClient) -> None:
    router = lab.app.state.ctx.router  # type: ignore[attr-defined]
    assert router.context_length("fake/reader") is None  # nothing serves it yet
    reader(lab, "reader-small", 4096)
    reader(lab, "reader-big", 16384)
    assert router.context_length("fake/reader") == 4096  # the smallest: any may answer
    assert router.context_length("fake/nobody") is None
    lab.patch("/profile", json={"default_model": "fake/reader"})
    item = ingest(lab)
    assert item["state"] == "carded", item["data"].get("error")
    data = item["data"]
    assert data["extraction"]["context_length"] == 4096
    assert data["extraction"]["characters_read"] < data["text"]["characters"]
    assert data["extraction"]["characters_read"] < papers.TEXT_FOR_MODEL
