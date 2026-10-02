"""Picking a Notion page to publish under (GET /connectors/notion/pages) and taking a
report away as one zip (GET /experiments/{id}/export). Notion is mocked here."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import wait_for
from fastapi.testclient import TestClient

NOTION = "ntn_" + "c" * 40
DASHED = "01234567-89ab-cdef-0123-456789abcdef"


def page(page_id: str, title: list[str], icon: dict[str, Any] | None,
         prop: str = "title") -> dict[str, Any]:  # fmt: skip
    return {
        "object": "page", "id": page_id, "url": f"https://www.notion.so/{page_id}",
        "icon": icon,
        "properties": {
            "Tags": {"id": "a", "type": "multi_select", "multi_select": []},
            prop: {"id": "title", "type": "title",
                   "title": [{"type": "text", "plain_text": t} for t in title]},
        },
    }  # fmt: skip


RESULTS = [
    page(DASHED, ["Fluid ", "dynamics"], {"type": "emoji", "emoji": "🌊"}),
    page("f" * 32, ["Uploaded icon"], {"type": "file", "file": {"url": "https://s3/x.png"}}),
    page("e" * 32, ["Linked icon"], {"type": "external", "external": {"url": "https://x/y"}}),
    page("d" * 32, ["  "], None, prop="Name"),
    {"object": "database", "id": "c" * 32, "title": []},  # not a page
]


class Notion:
    """Notion's search, as far as Newton uses it."""

    def __init__(self, status: int = 200, down: bool = False, raw: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.status = status
        self.down = down
        self.raw = raw  # a 200 with this body instead of a search result

    def factory(self) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            self.calls.append((str(request.url), body, dict(request.headers)))
            if self.down:
                raise httpx.ConnectError(f"no route to Notion (Bearer {NOTION})")
            if self.status != 200:
                return httpx.Response(self.status, json={
                    "object": "error", "status": self.status, "code": "unauthorized",
                    "message": f"API token is invalid: {NOTION}"})  # fmt: skip
            if self.raw is not None:
                return httpx.Response(200, text=self.raw)
            return httpx.Response(200, json={"object": "list", "results": RESULTS,
                                             "has_more": False})  # fmt: skip

        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def use(client: TestClient, notion: Notion) -> Notion:
    client.app.state.ctx.publisher.http_factory = notion.factory()  # type: ignore[attr-defined]
    return notion


def test_notion_pages(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    notion = use(client, Notion())
    client.put("/connectors/notion", json={"token": NOTION})
    r = client.get("/connectors/notion/pages", params={"query": "  fluid "})
    assert r.status_code == 200
    assert r.json() == [
        {"id": DASHED.replace("-", ""), "title": "Fluid dynamics",
         "url": f"https://www.notion.so/{DASHED}", "icon": "🌊"},
        {"id": "f" * 32, "title": "Uploaded icon", "url": f"https://www.notion.so/{'f' * 32}",
         "icon": None},
        {"id": "e" * 32, "title": "Linked icon", "url": f"https://www.notion.so/{'e' * 32}",
         "icon": None},
        {"id": "d" * 32, "title": "Untitled", "url": f"https://www.notion.so/{'d' * 32}",
         "icon": None},
    ]  # fmt: skip
    url, body, headers = notion.calls[0]
    assert url == "https://api.notion.com/v1/search"
    assert body == {
        "query": "fluid", "page_size": 50, "filter": {"property": "object", "value": "page"},
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
    }  # fmt: skip
    assert headers["authorization"] == f"Bearer {NOTION}"
    assert headers["notion-version"] == "2022-06-28"
    client.get("/connectors/notion/pages")
    assert "query" not in notion.calls[1][1]  # no query: everything, last edited first
    assert NOTION not in r.text and NOTION not in caplog.text


def test_notion_pages_need_a_connection(client: TestClient) -> None:
    notion = use(client, Notion())
    r = client.get("/connectors/notion/pages")
    assert r.status_code == 409
    assert r.json() == {"error": "Notion isn't connected", "code": "not_connected"}
    assert notion.calls == []  # nothing asked of Notion without a token
    assert client.get("/connectors/notion/pages", params={"query": "x" * 201}).status_code == 422


def test_notion_refusals_are_502_without_the_token(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client.put("/connectors/notion", json={"token": NOTION})
    use(client, Notion(status=401))
    r = client.get("/connectors/notion/pages")
    assert r.status_code == 502
    assert r.json()["error"] == "Notion answered 401: API token is invalid: <token>"
    use(client, Notion(down=True))
    down = client.get("/connectors/notion/pages")
    assert down.status_code == 502 and "Notion couldn't be reached" in down.json()["error"]
    for text in (r.text, down.text, caplog.text):
        assert NOTION not in text


@pytest.mark.parametrize("raw", ["<html>", "", "null", "[]", '{"results": null}'])
def test_a_notion_200_that_isnt_a_search_result_is_502(client: TestClient, raw: str) -> None:
    client.put("/connectors/notion", json={"token": NOTION})
    use(client, Notion(raw=raw))  # a captive portal or proxy in front of api.notion.com
    r = client.get("/connectors/notion/pages")
    assert r.status_code == 502
    assert r.json()["error"] == "Notion answered 200 with something that isn't a search result"


# -- export -------------------------------------------------------------------------------


def experiment(client: TestClient, title: str = "Upwind vs. Lax–Wendroff / 1D!") -> str:
    r = client.post("/experiments", json={
        "title": title, "host_id": "local", "backend": "cpu",
        "variants": [
            {"role": "baseline", "label": "upwind",
             "params": {"scheme": "upwind", "resolutions": [32, 64], "repeats": 1}},
            {"role": "candidate", "label": "lw",
             "params": {"scheme": "lax_wendroff", "resolutions": [32, 64], "repeats": 1}},
        ]})  # fmt: skip
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def reported(client: TestClient) -> str:
    exp = experiment(client)
    approval = next(a for a in client.get("/approvals?status=pending").json()
                    if a["subject_id"] == exp)  # fmt: skip
    client.post(f"/approvals/{approval['id']}/approve")
    wait_for(lambda: client.get(f"/experiments/{exp}").json()["state"],
             lambda s: s == "reported", timeout=120)  # fmt: skip
    return exp


def test_export_zip(client: TestClient, tmp_path: Path) -> None:
    exp = reported(client)
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    report_dir = Path(ctx.settings.reports_dir) / exp
    (report_dir / "data").mkdir(exist_ok=True)
    (report_dir / "data" / "table.csv").write_text("n,err\n32,0.1\n")
    secret = tmp_path / "outside.txt"
    secret.write_text("not part of any report")
    (report_dir / "leak.txt").symlink_to(secret)  # a link out of the report: left out
    (report_dir / "leakdir").symlink_to(tmp_path, target_is_directory=True)

    r = client.get(f"/experiments/{exp}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == (
        f'attachment; filename="upwind-vs-lax-wendroff-1d-{exp}.zip"'
    )
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert names[:2] == ["report.md", "report.json"] and len(set(names)) == len(names)
    assert zf.read("report.md").decode() == client.get(f"/experiments/{exp}/report").text
    stored = client.get(f"/experiments/{exp}").json()["evaluation"]
    assert json.loads(zf.read("report.json")) == stored
    assert "data/table.csv" in names
    on_disk = {p.relative_to(report_dir).as_posix() for p in report_dir.rglob("*")
               if p.is_file() and "leak" not in p.as_posix()}  # fmt: skip
    assert set(names) == on_disk  # report.md, report.json and every file of the report
    for name in names:
        assert not name.startswith("/") and ".." not in Path(name).parts
        assert "leak" not in name
        if name not in ("report.md", "report.json"):  # what the files route serves
            served = client.get(f"/experiments/{exp}/report/files/{name}")
            assert served.status_code == 200 and served.content == zf.read(name)
    assert b"not part of any report" not in b"".join(zf.read(n) for n in names)


def test_export_names_and_refusals(client: TestClient) -> None:
    assert client.get("/experiments/exp_nope/export").status_code == 404
    assert client.get("/experiments/exp_nope/export").json() == {"error": "no experiment exp_nope"}
    exp = experiment(client, title="—")  # awaiting approval: nothing reported yet
    r = client.get(f"/experiments/{exp}/export")
    assert r.status_code == 409 and r.json() == {"error": "the experiment has no report yet"}
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    from newton_agentd.reporting.export import export_name

    row = ctx.experiments.get_row(exp)
    assert export_name(row) == f"experiment-{exp}.zip"  # nothing left of the title
    assert export_name({**row, "title": 'a"b\\c\r\n' + "x" * 200}).startswith("a-b-c-xxx")
    assert len(export_name({**row, "title": "x" * 200})) == 61 + len(exp) + 4
