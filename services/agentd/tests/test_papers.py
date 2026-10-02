"""B4: an arXiv paper becomes a research card (offline: arXiv is mocked, the model is
the fake engine answering with a fixed card), and a card proposes an experiment."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import wait_for
from fastapi.testclient import TestClient
from newton_agentd.research import papers
from test_services_api import FAKE, stop_services

AID = "2401.01234"
ATOM = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/{AID}v1</id>
    <published>2024-01-02T00:00:00Z</published>
    <title>A Koren-limited  scheme for
      linear advection</title>
    <summary>We propose a flux-limited Lax-Wendroff scheme with the Koren limiter.</summary>
    <author><name>A. Author</name></author><author><name>B. Author</name></author>
    <category term="physics.comp-ph"/><category term="math.NA"/>
  </entry>
</feed>""".encode()
HTML = ("<html><head><style>x{}</style><script>evil()</script></head><body><nav>menu</nav>"
        "<h1>Introduction</h1><p>We solve <math alttext='u_t + a u_x = 0'><mi>u</mi></math> "
        "with a flux limiter.</p>" + "<p>The scheme is TVD for CFL up to one. " * 100
        + "</p></body></html>").encode()  # fmt: skip
CARD = {
    "relevant": True,
    "summary": "A Koren-limited Lax-Wendroff scheme, second order and TVD.",
    "method": {"name": "Koren TVD", "limiter": "koren", "second_order_correction": True,
               "time_integration": "one_step", "order": 2, "max_cfl": 1.0, "tvd": True},
    "claims": [{"kind": "order", "text": "Second order on smooth data."},
               {"kind": "tvd", "text": "No new extrema for CFL <= 1."}],
    "benchmarks": ["square wave advection"],
}  # fmt: skip


def fake_arxiv(html: bytes | None = HTML, pdf: bytes | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "export.arxiv.org" in url:
            return httpx.Response(
                200, content=ATOM, headers={"content-type": "application/atom+xml"}
            )
        if "/html/" in url:
            return (httpx.Response(200, content=html, headers={"content-type": "text/html"})
                    if html else httpx.Response(404))  # fmt: skip
        if "/pdf/" in url:
            return (httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
                    if pdf else httpx.Response(404))  # fmt: skip
        return httpx.Response(404)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def lab(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    client.app.state.ctx.papers.http_factory = fake_arxiv()  # type: ignore[attr-defined]
    yield client
    stop_services(client)


def reader(client: TestClient, card: Any = CARD) -> str:
    """A model service answering with this card; the profile's default model."""
    reply = card if isinstance(card, str) else json.dumps(card)
    r = client.post("/services", json={"host_id": "local", "name": "reader", "settings": {
        **FAKE, "model": "fake/reader", "fake": {"reply_text": reply}}})  # fmt: skip
    wait_for(lambda: client.get(f"/services/{r.json()['id']}").json(),
             lambda s: (s.get("endpoint") or {}).get("reachable"), timeout=60)  # fmt: skip
    client.patch("/profile", json={"default_model": "fake/reader"})
    return str(r.json()["id"])


def ingest(client: TestClient, ref: str = f"https://arxiv.org/abs/{AID}v2") -> dict[str, Any]:
    r = client.post("/research/ingest", json={"ref": ref})
    assert r.status_code == 202, r.text
    item_id = r.json()["id"]
    done: dict[str, Any] = wait_for(lambda: client.get(f"/research/items/{item_id}").json(),
                                    lambda i: i["state"] in ("carded", "failed"),
                                    timeout=30)  # fmt: skip
    return done


@pytest.mark.parametrize(("ref", "aid"), [
    ("2401.01234", "2401.01234"), ("arXiv:2401.01234v3", "2401.01234"),
    ("https://arxiv.org/pdf/2401.01234v1", "2401.01234"),
    ("https://arxiv.org/html/2401.01234", "2401.01234"),
    ("math.NA/0601001", "math.NA/0601001"),
    ("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001"),
])  # fmt: skip
def test_arxiv_ids(ref: str, aid: str) -> None:
    assert papers.arxiv_id(ref) == aid


def test_not_an_arxiv_reference() -> None:
    for ref in ("hello", "https://example.com/1234.5678", "../../etc/passwd"):
        with pytest.raises(papers.PaperError):
            papers.arxiv_id(ref)


def test_a_paper_becomes_a_card_and_a_scheme(lab: TestClient) -> None:
    service = reader(lab)
    item = ingest(lab)
    assert item["state"] == "carded", item["data"].get("error")
    data = item["data"]
    assert item["title"] == "A Koren-limited scheme for linear advection"
    assert data["paper"]["authors"] == ["A. Author", "B. Author"]
    assert data["paper"]["categories"] == ["physics.comp-ph", "math.NA"]
    assert data["text"]["from"] == "html" and data["text"]["characters"] > 2000
    text = Path(data["text"]["path"]).read_text(encoding="utf-8")
    assert "$u_t + a u_x = 0$" in text and "evil()" not in text and "menu" not in text
    assert data["card"]["method"]["limiter"] == "koren"
    assert data["scheme_ir"]["flux"]["limiter"] == "koren"
    assert data["scheme_ir"]["claims"] == {"order": 2, "max_cfl": 1.0, "tvd": True}
    assert data["scheme_ir_digest"] and data["scheme_note"] == "mapped onto Newton's IR"
    prov = data["extraction"]
    assert prov["service"] == service and prov["model"] == "fake/reader"
    logged = [r for r in lab.get("/router/requests").json() if r["id"] == prov["request-id"]]
    assert logged and logged[0]["path"].endswith("(newton)")  # through the router
    again = lab.post("/research/ingest", json={"ref": AID}).json()
    assert again["id"] == item["id"]  # the same paper once


def test_the_pdf_when_there_is_no_html(lab: TestClient) -> None:
    lab.app.state.ctx.papers.http_factory = fake_arxiv(
        html=None,
        pdf=tiny_pdf(  # type: ignore[attr-defined]
            "A flux limited scheme for advection. " * 40
        ),
    )
    reader(lab)
    item = ingest(lab)
    assert item["state"] == "carded", item["data"].get("error")
    assert item["data"]["text"]["from"] == "pdf"


def test_what_the_model_says_stays_data(lab: TestClient) -> None:
    sneaky = {**CARD, "method": {**CARD["method"], "limiter": "__import__('os').system('x')",
                                 "time_integration": "rk4; rm -rf /"}}  # fmt: skip
    reader(lab, sneaky)
    item = ingest(lab)
    assert item["state"] == "carded"
    assert item["data"]["card"]["method"]["limiter"] == "other"
    assert item["data"]["scheme_ir"] is None and "vocabulary" in item["data"]["scheme_note"]
    r = lab.post(f"/research/items/{item['id']}/propose", json={})
    assert r.status_code == 400 and "maps onto" in r.json()["error"]


def test_a_model_that_does_not_answer_json_fails_the_item(lab: TestClient) -> None:
    reader(lab, "I am sorry, I can't read papers.")
    item = ingest(lab)
    assert item["state"] == "failed" and "JSON" in item["data"]["error"]
    retry = lab.post("/research/ingest", json={"ref": AID})
    assert retry.json()["id"] == item["id"]  # a failed item can be tried again


def test_without_a_model_nothing_starts(lab: TestClient) -> None:
    r = lab.post("/research/ingest", json={"ref": AID})
    assert r.status_code == 400 and "no model" in r.json()["error"]


def test_a_card_proposes_an_experiment_that_runs_after_approval(lab: TestClient) -> None:
    reader(lab)
    item = ingest(lab)
    r = lab.post(f"/research/items/{item['id']}/propose",
                 json={"host_id": "local", "backend": "cpu"})  # fmt: skip
    assert r.status_code == 201, r.text
    exp = r.json()
    assert exp["state"] == "awaiting_approval" and exp["research_item_id"] == item["id"]
    assert lab.get(f"/research/items/{item['id']}").json()["state"] == "experiment_planned"
    approval = next(a for a in lab.get("/approvals?status=pending").json()
                    if a["subject_id"] == exp["id"])  # fmt: skip
    assert "Koren" in approval["details"]["hypothesis"] or "koren" in approval["title"]
    lab.post(f"/approvals/{approval['id']}/approve")
    done = wait_for(lambda: lab.get(f"/experiments/{exp['id']}").json(),
                    lambda e: e["state"] in ("reported", "failed"), timeout=180)  # fmt: skip
    assert done["state"] == "reported", done
    claims = next(c for c in done["evaluation"]["verdicts"][0]["checks"] if c["name"] == "claims")
    assert claims["passed"] is True  # Koren: second order, stable at 1, TVD


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with real text (no PDF library needed to write it)."""
    words = text.replace("(", "").replace(")", "")
    lines = [words[i : i + 90] for i in range(0, len(words), 90)][:40]
    stream = "BT /F1 9 Tf 40 800 Td 11 TL " + " ".join(f"({ln}) '" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out
