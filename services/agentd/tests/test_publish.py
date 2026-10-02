"""B6: reports to GitHub and Notion, always approved first; exactly the approved text
is sent; tokens only in the secret store. GitHub and Notion are mocked here."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd.connectors.publish import notion_blocks

TOKEN = "ghp_" + "a" * 36
NOTION = "ntn_" + "b" * 40
PAGE = "0123456789abcdef0123456789abcdef"


class Remote:
    """GitHub's and Notion's APIs, as far as Newton uses them."""

    def __init__(self, fail: int | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.fail = fail

    def factory(self) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            self.calls.append((request.method, str(request.url), body, dict(request.headers)))
            if self.fail:
                return httpx.Response(self.fail, json={"message": f"no, {TOKEN}"})
            url = str(request.url)
            if url.endswith("/gists"):
                return httpx.Response(201, json={"html_url": "https://gist.github.com/x/1"})
            if "/issues" in url:
                return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/7"})
            if url.endswith("/pages"):
                return httpx.Response(200, json={"id": "page-1", "url": "https://notion.so/p1"})
            if "/children" in url:
                return httpx.Response(200, json={})
            return httpx.Response(404)

        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def remote(client: TestClient) -> Iterator[Remote]:
    r = Remote()
    client.app.state.ctx.publisher.http_factory = r.factory()  # type: ignore[attr-defined]
    yield r


def reported(client: TestClient) -> str:
    r = client.post("/experiments", json={
        "title": "upwind vs lax-wendroff", "host_id": "local", "backend": "cpu",
        "variants": [
            {"role": "baseline", "label": "upwind",
             "params": {"scheme": "upwind", "resolutions": [32, 64], "repeats": 1}},
            {"role": "candidate", "label": "lw",
             "params": {"scheme": "lax_wendroff", "resolutions": [32, 64], "repeats": 1}},
        ]})  # fmt: skip
    approval = client.get("/approvals?status=pending").json()[0]
    client.post(f"/approvals/{approval['id']}/approve")
    wait_for(lambda: client.get(f"/experiments/{r.json()['id']}").json()["state"],
             lambda s: s == "reported", timeout=120)  # fmt: skip
    return str(r.json()["id"])


def decide(client: TestClient, pub: dict[str, Any], approve: bool = True) -> dict[str, Any]:
    approval = next(a for a in client.get("/approvals?status=pending").json()
                    if a["subject_id"] == pub["id"])  # fmt: skip
    client.post(f"/approvals/{approval['id']}/{'approve' if approve else 'reject'}")
    done: dict[str, Any] = wait_for(
        lambda: next(p for p in client.get("/publications").json() if p["id"] == pub["id"]),
        lambda p: p["state"] in ("published", "failed", "rejected"), timeout=30)  # fmt: skip
    return done


def test_nothing_leaves_the_mac_before_approval(client: TestClient, remote: Remote) -> None:
    exp = reported(client)
    assert client.post(f"/experiments/{exp}/publish", json={"target": "github"}).status_code == 400
    assert client.put("/connectors/github", json={"token": TOKEN}).json()["github"] is True
    pub = client.post(f"/experiments/{exp}/publish", json={"target": "github"}).json()
    assert pub["state"] == "awaiting_approval" and remote.calls == []
    approval = next(a for a in client.get("/approvals?status=pending").json()
                    if a["subject_id"] == pub["id"])  # fmt: skip
    assert "secret GitHub gist" in approval["title"]
    assert approval["details"]["preview"].startswith("# ")  # what will be sent, shown
    rejected = decide(client, pub, approve=False)
    assert rejected["state"] == "rejected" and remote.calls == []


def test_a_gist_gets_exactly_the_approved_report(client: TestClient, remote: Remote) -> None:
    exp = reported(client)
    client.put("/connectors/github", json={"token": TOKEN})
    pub = client.post(f"/experiments/{exp}/publish", json={"target": "github"}).json()
    done = decide(client, pub)
    assert done["state"] == "published" and done["url"] == "https://gist.github.com/x/1"
    method, url, body, headers = remote.calls[0]
    assert url == "https://api.github.com/gists" and body["public"] is False
    sent = body["files"]["newton-report.md"]["content"]
    import hashlib

    assert hashlib.sha256(sent.encode()).hexdigest() == pub["content_sha256"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    stored = b"".join(p.read_bytes() for p in Path(ctx.settings.data_dir).glob("newton.db*"))
    assert TOKEN.encode() not in stored  # the token: secret store only


def test_an_issue_and_a_tampered_report(client: TestClient, remote: Remote) -> None:
    exp = reported(client)
    client.put("/connectors/github", json={"token": TOKEN})
    sneaky = {"target": "github", "destination": {"kind": "issue", "repo": "o/r; rm -rf"}}
    bad = client.post(f"/experiments/{exp}/publish", json=sneaky)
    assert bad.status_code == 400
    pub = client.post(f"/experiments/{exp}/publish", json={
        "target": "github", "destination": {"kind": "issue", "repo": "o/r"}}).json()  # fmt: skip
    assert decide(client, pub)["url"] == "https://github.com/o/r/issues/7"
    assert remote.calls[0][1] == "https://api.github.com/repos/o/r/issues"
    second = client.post(f"/experiments/{exp}/publish", json={"target": "github"}).json()
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    frozen = Path(ctx.settings.data_dir) / "publications" / f"{second['id']}.md"
    frozen.write_text(frozen.read_text() + "\nsomething nobody approved\n")
    failed = decide(client, second)
    assert failed["state"] == "failed" and "changed after it was approved" in failed["error"]
    assert len(remote.calls) == 1  # the tampered one was never sent


def test_notion_pages_in_batches(client: TestClient, remote: Remote) -> None:
    exp = reported(client)
    client.put("/connectors/notion", json={"token": NOTION})
    assert client.post(f"/experiments/{exp}/publish",
                       json={"target": "notion", "destination": {}}).status_code == 400  # fmt: skip
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    row = ctx.db.query_one("SELECT report_path FROM experiments WHERE id = ?", (exp,))
    long = Path(row["report_path"]).read_text() + "\n".join(f"line {i}" for i in range(250))
    Path(row["report_path"]).write_text(long)  # a long report: more than 100 blocks
    pub = client.post(f"/experiments/{exp}/publish", json={
        "target": "notion", "destination": {"parent_page_id": PAGE}}).json()  # fmt: skip
    assert decide(client, pub)["url"] == "https://notion.so/p1"
    first = remote.calls[0]
    assert first[2]["parent"] == {"page_id": PAGE} and len(first[2]["children"]) == 100
    assert first[3]["notion-version"] == "2022-06-28"
    assert any("/blocks/page-1/children" in c[1] for c in remote.calls[1:])


def test_a_refusal_is_reported_without_the_token(client: TestClient) -> None:
    exp = reported(client)
    failing = Remote(fail=401)
    client.app.state.ctx.publisher.http_factory = failing.factory()  # type: ignore[attr-defined]
    client.put("/connectors/github", json={"token": TOKEN})
    done = decide(client, client.post(f"/experiments/{exp}/publish",
                                      json={"target": "github"}).json())  # fmt: skip
    assert done["state"] == "failed" and "401" in done["error"] and TOKEN not in done["error"]


def test_connectors(client: TestClient) -> None:
    assert client.get("/connectors").json() == {"github": False, "notion": False}
    assert client.put("/connectors/github", json={"token": "short"}).status_code == 422
    assert client.put("/connectors/notion", json={"token": "x" * 20 + "; rm"}).status_code == 400
    client.put("/connectors/notion", json={"token": NOTION})
    assert client.get("/connectors").json() == {"github": False, "notion": True}
    assert client.delete("/connectors/notion").json() == {"github": False, "notion": False}


def test_markdown_to_notion_blocks() -> None:
    blocks = notion_blocks("# Title\n\nSome text.\n\n- a point\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
                           "## Section\n" + "x" * 4500)  # fmt: skip
    kinds = [b["type"] for b in blocks]
    assert kinds == ["heading_1", "paragraph", "bulleted_list_item", "code", "heading_2",
                     "paragraph"]  # fmt: skip
    assert blocks[3]["code"]["rich_text"][0]["text"]["content"].count("|") == 9  # a table
    assert len(blocks[-1]["paragraph"]["rich_text"]) == 3  # 2000-character pieces
