"""Paper ingestion (B4): an arXiv paper becomes a research card.

    arXiv id or URL -> metadata (arXiv's API) -> full text (arXiv's HTML, or the PDF)
    -> a model, through Newton's router, fills a small JSON card -> Newton maps the
    card's method onto a SchemeIR (engine E2), which an experiment can then test.

The model never writes code, nor even the IR: it picks from fixed vocabularies
(limiter, time integration, claimed order, CFL, TVD) and Newton's own mapping turns
those choices into a document. Anything it can't map is kept as a note, never run.
Text and model output are data: nothing from a paper or a model is ever executed.

States (research_item): discovered -> extracting -> carded | failed; a card can then
propose an experiment (experiment_planned), and so can a reported paper (a new test).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import html.parser
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..network import NetworkState
from ..orchestration.state_machine import RESEARCH_ITEM, ConcurrentTransition, record_event
from ..storage.db import Database, Row, dumps, loads, new_id, now
from . import schemes

log = logging.getLogger("newton_agentd.papers")

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_HTML = "https://arxiv.org/html/{id}"
ARXIV_PDF = "https://arxiv.org/pdf/{id}"
NEW_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
OLD_ID = re.compile(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
MAX_DOWNLOAD = 40 * 1024 * 1024
TEXT_FOR_MODEL = 24_000  # characters of the paper the model reads (abstract + body)
CARD_TOKENS = 1200  # the model's answer (max_tokens)
CHARS_PER_TOKEN = 3  # LaTeX-heavy text: fewer characters per token than prose
CONTEXT_MARGIN = 256  # tokens kept free: chat template, tokenizers that count differently
MIN_TEXT = 1000  # less of the paper than this isn't worth asking a model about
RETRY_DELAY = 3.0  # seconds before the one retry (no sooner than arXiv's API pace)
UNREACHABLE = "arXiv couldn't be reached: check the network"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

LIMITER_CHOICES = ("none", "minmod", "van_leer", "superbee", "mc", "koren", "other")
TIME_CHOICES = ("one_step", "ssprk2", "ssprk3", "other")
CLAIM_KINDS = ("order", "stability", "tvd", "conservation", "accuracy", "speed", "other")
TABLEAUS = {
    "ssprk2": {"a": [[0, 0], [1, 0]], "b": [0.5, 0.5]},
    "ssprk3": {"a": [[0, 0, 0], [1, 0, 0], [0.25, 0.25, 0]], "b": [1 / 6, 1 / 6, 2 / 3]},
}
LW = {"op": "mul", "args": [0.5, {"op": "sub", "args": [1.0, "c"]}]}

PROMPT = """You read numerical-methods papers for a research assistant. Answer with ONE JSON
object and nothing else, with exactly these fields:

{
  "relevant": true or false,  // is the paper about a finite-volume / finite-difference
                              // scheme for advection or hyperbolic conservation laws?
  "summary": "at most three sentences: what the paper proposes and shows",
  "method": {
    "name": "short name of the proposed scheme",
    "limiter": one of %(limiters)s,
    "second_order_correction": true or false,  // a Lax-Wendroff / MUSCL-type correction
    "time_integration": one of %(times)s,
    "order": an integer, the formal order of accuracy the paper claims,
    "max_cfl": a number, the largest stable Courant number the paper claims,
    "tvd": true or false, does the paper claim the scheme is TVD
  },
  "claims": [{"kind": one of %(kinds)s, "text": "one claim, in a sentence"}],
  "benchmarks": ["test problems the paper uses, e.g. linear advection of a square wave"]
}

Use "other" when none of the choices fits. Do not invent claims the paper does not make."""


class PaperError(ValueError):
    pass


class ArxivUnreachable(PaperError):
    """No answer from arXiv at all (no network, DNS, refused, timed out). `offline`:
    the failure was connection-level, as NetworkState classifies it."""

    def __init__(self, message: str, *, offline: bool = False) -> None:
        super().__init__(message)
        self.offline = offline


def unreachable(http: httpx.AsyncClient, exc: httpx.TransportError,
                offline: bool) -> ArxivUnreachable:  # fmt: skip
    """A transport failure as a sentence (no exception text: it can be empty)."""
    if isinstance(exc, httpx.TimeoutException):
        timeout = http.timeout
        limit = timeout.connect if isinstance(exc, httpx.ConnectTimeout) else timeout.read
        if limit:
            return ArxivUnreachable(f"arXiv didn't answer within {limit:g} s", offline=offline)
    return ArxivUnreachable(UNREACHABLE, offline=offline)


def text_budget(context_length: int | None, prompt_chars: int) -> int:
    """How much of the paper the model reads: TEXT_FOR_MODEL at most, and no more than
    fits its context next to the prompt and the answer (all of it when unknown)."""
    if not context_length:
        return TEXT_FOR_MODEL
    room = (context_length - CARD_TOKENS - CONTEXT_MARGIN) * CHARS_PER_TOKEN - prompt_chars
    return max(0, min(TEXT_FOR_MODEL, room))


def _tokens_used(body: dict[str, Any]) -> int:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0
    counts = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return sum(v for v in counts if isinstance(v, int) and not isinstance(v, bool))


def arxiv_id(text: str) -> str:
    """The arXiv id in an id or an arxiv.org URL (abs, pdf, html), without version."""
    text = text.strip()
    for pattern in (NEW_ID, OLD_ID):
        m = pattern.search(text)
        if m and ("arxiv" in text.lower() or m.group(0) == text or text.startswith(m.group(1))):
            return m.group(1)
    raise PaperError(f"not an arXiv id or URL: {text[:100]!r}")


# -- fetching ---------------------------------------------------------------------------


async def _get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    async with http.stream("GET", url, follow_redirects=True) as resp:
        if resp.status_code != 200:
            return resp
        chunks, total = [], 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise PaperError(f"{url} is larger than {MAX_DOWNLOAD // 2**20} MB")
            chunks.append(chunk)
        resp._content = b"".join(chunks)  # noqa: SLF001 - read once, with a size cap
        return resp


def parse_atom(xml: bytes, aid: str) -> dict[str, Any]:
    root = ET.fromstring(xml)  # noqa: S314 - arXiv's own API response, size-capped
    entry = root.find("a:entry", ATOM)
    if entry is None or entry.find("a:title", ATOM) is None:
        raise PaperError(f"arXiv has no paper {aid}")
    return parse_entry(entry, aid)


def parse_feed(xml: bytes) -> list[dict[str, Any]]:
    """Every paper in an arXiv API search result (PaperError: not a search result)."""
    try:
        root = ET.fromstring(xml)  # noqa: S314 - arXiv's own API response, size-capped
    except ET.ParseError:
        root = None
    if root is None or root.tag != f"{{{ATOM['a']}}}feed":  # a captive portal, a proxy
        raise PaperError("arXiv's answer wasn't a search result (a captive portal?)")
    out = []
    for entry in root.findall("a:entry", ATOM):
        raw_id = (entry.findtext("a:id", "", ATOM) or "").strip()
        try:
            aid = arxiv_id(raw_id)
        except PaperError:
            continue
        with contextlib.suppress(PaperError):
            out.append(parse_entry(entry, aid))
    return out


def parse_entry(entry: ET.Element, aid: str) -> dict[str, Any]:

    def text(tag: str) -> str:
        node = entry.find(tag, ATOM)
        return " ".join((node.text or "").split()) if node is not None else ""

    if text("a:id").endswith("/api/errors") or not text("a:title"):
        raise PaperError(f"arXiv has no paper {aid}")
    categories = [c.get("term", "") for c in entry.findall("a:category", ATOM)]
    return {
        "arxiv_id": aid,
        "title": text("a:title"),
        "abstract": text("a:summary"),
        "authors": [
            " ".join((a.findtext("a:name", "", ATOM)).split())
            for a in entry.findall("a:author", ATOM)
        ],  # fmt: skip
        "published": text("a:published"),
        "categories": categories,
        "url": f"https://arxiv.org/abs/{aid}",
        "journal_ref": text("arxiv:journal_ref") or None,
        "doi": text("arxiv:doi") or None,
    }


class _Text(html.parser.HTMLParser):
    """Visible text of an arXiv HTML paper: no scripts, styles, nav or math markup."""

    SKIP = {"script", "style", "nav", "header", "footer", "annotation", "annotation-xml"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in ("p", "div", "section", "h1", "h2", "h3", "h4", "li", "br", "tr"):
            self.parts.append("\n")
        if tag == "math":  # keep formulas readable: their alttext is the LaTeX
            alt = dict(attrs).get("alttext")
            if alt:
                self.parts.append(f" ${alt}$ ")
                self.skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if (tag in self.SKIP or tag == "math") and self.skipping:
            self.skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_text(raw: bytes) -> str:
    parser = _Text()
    parser.feed(raw.decode("utf-8", "replace"))
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages[:60]).strip()


# -- the model's card -> Newton's IR -------------------------------------------------------


def parse_card(content: str) -> dict[str, Any]:
    """The model's JSON card, checked field by field (anything unexpected is dropped)."""
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise PaperError("the model did not answer with a JSON object")
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        raise PaperError("the model's answer is not valid JSON") from None
    if not isinstance(raw, dict):
        raise PaperError("the model's answer is not a JSON object")
    method: dict[str, Any] = raw["method"] if isinstance(raw.get("method"), dict) else {}

    def pick(value: Any, choices: tuple[str, ...]) -> str:
        return value if value in choices else "other"

    order = method.get("order")
    max_cfl = method.get("max_cfl")
    card = {
        "relevant": raw.get("relevant") is True,
        "summary": str(raw.get("summary") or "")[:1200],
        "method": {
            "name": str(method.get("name") or "")[:120],
            "limiter": pick(method.get("limiter"), LIMITER_CHOICES),
            "second_order_correction": method.get("second_order_correction") is True,
            "time_integration": pick(method.get("time_integration"), TIME_CHOICES),
            "order": order if isinstance(order, int) and 1 <= order <= 4 else None,
            "max_cfl": _cfl(max_cfl),
            "tvd": method.get("tvd") is True,
        },
        "claims": [
            {"kind": pick(c.get("kind"), CLAIM_KINDS), "text": str(c.get("text") or "")[:400]}
            for c in (raw.get("claims") or [])[:20]
            if isinstance(c, dict) and c.get("text")
        ],
        "benchmarks": [
            str(b)[:200] for b in (raw.get("benchmarks") or [])[:10] if isinstance(b, str)
        ],  # fmt: skip
    }
    return card


def _cfl(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < value <= 2 else None


def scheme_from_card(card: dict[str, Any], slug: str) -> tuple[dict[str, Any] | None, str]:
    """Newton's own mapping from the card's choices to a SchemeIR (or why not)."""
    m = card["method"]
    if not card["relevant"]:
        return None, "the paper is not about a scheme Newton's advection benchmark can run"
    if m["limiter"] == "other" or m["time_integration"] == "other":
        return None, ("its limiter or time integration is outside Newton's IR vocabulary "
                      "(kept as a note: a new IR element would be needed)")  # fmt: skip
    if m["order"] is None or m["max_cfl"] is None:
        return None, "the paper's claimed order or CFL limit could not be read"
    corrected = m["second_order_correction"] or m["limiter"] != "none"
    flux: dict[str, Any] = {"limiter": m["limiter"], "correction": None}
    time: dict[str, Any] = {"method": "one_step"}
    if corrected and m["time_integration"] == "one_step":
        flux["correction"] = LW  # Lax-Wendroff in time: A(c) = (1 - c) / 2
    elif corrected:
        flux["correction"] = 0.5  # semi-discrete MUSCL: the RK method advances in time
        time = {"method": "rk", "tableau": TABLEAUS[m["time_integration"]]}
    elif m["limiter"] != "none":
        return None, "a limiter without a correction to limit"
    doc = {
        "name": slug,
        "description": f"{m['name']}: mapped by Newton from the paper's card",
        "flux": flux,
        "time": time,
        "claims": {"order": m["order"], "max_cfl": m["max_cfl"], "tvd": m["tvd"]},
    }
    return doc, "mapped onto Newton's IR"


def slugify(name: str, aid: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()[:40] or "scheme"
    return f"{base}_{re.sub(r'[^0-9A-Za-z]', '', aid)}"[:64]


# -- the pipeline ---------------------------------------------------------------------------


class Papers:
    def __init__(self, settings: Settings, db: Database, router: Any, profile: Any,
                 network: NetworkState | None = None) -> None:  # fmt: skip
        self.settings = settings
        self.db = db
        self.router = router
        self.profile = profile
        self.network = network or NetworkState()
        self.retry_delay = RETRY_DELAY
        self._tasks: set[asyncio.Task[None]] = set()
        self.http_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15),
            headers={"User-Agent": "Newton research agent (local, single user)"},
        )

    @property
    def text_dir(self) -> Path:
        return self.settings.data_dir / "papers"

    def get(self, item_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM research_items WHERE id = ?", (item_id,))
        if row is None:
            raise KeyError(item_id)
        return self.view(row)

    @staticmethod
    def view(row: Row) -> dict[str, Any]:
        out = dict(row)
        out["data"] = loads(row["data"]) or {}
        return out

    def list(self, goal_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM research_items WHERE (? IS NULL OR goal_id = ?) "
            "ORDER BY created_at DESC",
            (goal_id, goal_id),
        )
        return [self.view(r) for r in rows]

    def ingest(
        self, ref: str, goal_id: str | None = None, model: str | None = None
    ) -> dict[str, Any]:
        """Start ingesting an arXiv paper (the same paper twice: the existing item)."""
        aid = arxiv_id(ref)
        existing = self.db.query_one(
            "SELECT * FROM research_items WHERE source = 'arxiv' AND external_id = ?", (aid,)
        )
        old = (loads(existing["data"]) or {}) if existing is not None else {}
        # The loop found it but couldn't triage it (offline, no reader): Read again.
        untriaged = existing is not None and existing["state"] == "discovered" and old.get("error")
        if existing is not None and existing["state"] != "failed" and not untriaged:
            return self.view(existing)
        if goal_id and not self.db.query_one("SELECT 1 FROM goals WHERE id = ?", (goal_id,)):
            raise PaperError(f"unknown goal {goal_id}")
        model = model or self.profile.get()["default_model"]
        if not model:
            raise PaperError("no model: pass one, or set default_model in the profile")
        t = now()
        meta: dict[str, Any] | None = None
        if existing is not None:  # a failed attempt, or an untriaged find: same item
            item_id = existing["id"]
            data: dict[str, Any] = {"model": model}
            if untriaged and isinstance(old.get("paper"), dict):  # metadata already known
                meta = old["paper"]
                data.update(paper=meta, found_by=old.get("found_by"))
            self.db.execute(
                "UPDATE research_items SET state = 'discovered', data = ?, updated_at = ? "
                "WHERE id = ?",
                (dumps(data), t, item_id),
            )
        else:
            item_id = new_id("paper")
            self.db.insert("research_items", {
                "id": item_id, "goal_id": goal_id, "kind": "paper", "title": f"arXiv:{aid}",
                "source": "arxiv", "external_id": aid, "state": "discovered",
                "data": dumps({"model": model}), "created_at": t, "updated_at": t,
            })  # fmt: skip
        record_event(self.db, "research_item", item_id, "ingest", {"arxiv_id": aid, "model": model})
        task = asyncio.get_running_loop().create_task(self._run(item_id, aid, model, meta))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.get(item_id)

    async def wait_idle(self) -> None:
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _update(self, item_id: str, **data: Any) -> None:
        row = self.db.query_one("SELECT data FROM research_items WHERE id = ?", (item_id,))
        merged = {**(loads(row["data"]) if row else {}), **data}
        self.db.execute("UPDATE research_items SET data = ?, updated_at = ? WHERE id = ?",
                        (dumps(merged), now(), item_id))  # fmt: skip

    def _move(self, item_id: str, src: str, dst: str, **fields: Any) -> None:
        with contextlib.suppress(ConcurrentTransition):
            RESEARCH_ITEM.transition(self.db, item_id, src, dst, fields)

    async def _run(self, item_id: str, aid: str, model: str,
                   meta: dict[str, Any] | None = None) -> None:  # fmt: skip
        """Fetch, read and card one paper (metadata already known when the research
        loop found it in a search). Never raises: a paper that fails is a result."""
        try:
            async with self.http_factory() as http:
                if meta is None:
                    meta = await self.fetch_metadata(http, aid)
                    self.db.execute("UPDATE research_items SET title = ? WHERE id = ?",
                                    (meta["title"][:300], item_id))  # fmt: skip
                    self._update(item_id, paper=meta)
                text, kind = await self.fetch_text(http, aid)
            self.text_dir.mkdir(parents=True, exist_ok=True)
            path = self.text_dir / f"{re.sub(r'[^0-9A-Za-z.]', '_', aid)}.txt"
            path.write_text(text, encoding="utf-8")
            digest = hashlib.sha256(text.encode()).hexdigest()
            self._update(item_id, text={"path": str(path), "from": kind,
                                        "characters": len(text), "sha256": digest})  # fmt: skip
            row = self.db.query_one("SELECT state FROM research_items WHERE id = ?", (item_id,))
            if row is None or row["state"] not in ("discovered", "triaged"):
                return  # dismissed (or deleted) meanwhile
            self._move(item_id, row["state"], "extracting")
            card, provenance = await self.extract(meta, text, model)
            scheme, note = scheme_from_card(
                card, slugify(card["method"]["name"] or meta["title"], aid)
            )
            checked = None
            if scheme is not None:
                try:
                    checked = schemes.check(self.settings.benchmarks_dir, scheme)
                except ValueError as e:
                    scheme, note = None, f"the mapped scheme broke an IR rule: {e}"
            self._update(item_id, card=card, extraction=provenance, scheme_note=note,
                         scheme_ir=checked["document"] if checked else None,
                         scheme_ir_digest=checked["digest"] if checked else None,
                         method_digest=checked["method_digest"] if checked else None)  # fmt: skip
            self._move(item_id, "extracting", "carded")
            record_event(self.db, "research_item", item_id, "carded",
                         {"relevant": card["relevant"], "scheme": bool(checked)})  # fmt: skip
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a paper that can't be read is a result, not a crash
            log.warning("paper %s: %s", aid, e)
            reason = str(e)[:500] or type(e).__name__
            self._update(item_id, error=reason)
            row = self.db.query_one("SELECT state FROM research_items WHERE id = ?", (item_id,))
            if row and row["state"] in ("discovered", "triaged", "extracting"):
                self._move(item_id, row["state"], "failed")

    async def process(self, item_id: str, aid: str, model: str, meta: dict[str, Any]) -> None:
        """The research loop's way in: card an item it found (awaited, not detached)."""
        await self._run(item_id, aid, model, meta)

    async def fetch(self, http: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET from arXiv, retried once (after retry_delay) when it couldn't connect or
        was busy (5xx, 429). No answer at all: ArxivUnreachable, a sentence. Every try
        tells NetworkState how it went."""
        for attempt in (1, 2):
            try:
                resp = await _get(http, url)
            except httpx.TransportError as e:  # no answer: network, DNS, timeout
                offline = self.network.note_failure("arxiv", e)
                if attempt == 1 and isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                    await asyncio.sleep(self.retry_delay)
                    continue
                raise unreachable(http, e, offline) from None
            self.network.note_ok("arxiv")
            if attempt == 1 and (resp.status_code == 429 or resp.status_code >= 500):
                await asyncio.sleep(self.retry_delay)
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns

    async def fetch_metadata(self, http: httpx.AsyncClient, aid: str) -> dict[str, Any]:
        resp = await self.fetch(http, f"{ARXIV_API}?id_list={aid}&max_results=1")
        if resp.status_code != 200:
            raise PaperError(f"arXiv's API answered {resp.status_code}")
        return parse_atom(resp.content, aid)

    async def fetch_text(self, http: httpx.AsyncClient, aid: str) -> tuple[str, str]:
        resp = await self.fetch(http, ARXIV_HTML.format(id=aid))
        if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
            text = html_text(resp.content)
            if len(text) > 2000:
                return text, "html"
        resp = await self.fetch(http, ARXIV_PDF.format(id=aid))
        if resp.status_code != 200:
            raise PaperError(f"no full text: arXiv answered {resp.status_code} for the PDF")
        text = await asyncio.to_thread(pdf_text, resp.content)
        if len(text) < 500:
            raise PaperError("the PDF has no extractable text")
        return text, "pdf"

    async def extract(self, meta: dict[str, Any], text: str, model: str
                      ) -> tuple[dict[str, Any], dict[str, Any]]:  # fmt: skip
        prompt = PROMPT % {"limiters": list(LIMITER_CHOICES), "times": list(TIME_CHOICES),
                           "kinds": list(CLAIM_KINDS)}  # fmt: skip
        head = f"Title: {meta['title']}\n\nAbstract: {meta['abstract']}\n\nPaper:\n"
        # The paper is cut to the reader's context: a prompt longer than the context is
        # truncated by the engine (or refused), and the card comes back cut, or empty.
        context = self.router.context_length(model)
        budget = text_budget(context, len(prompt) + len(head))
        if budget < min(MIN_TEXT, len(text)):
            raise PaperError(f"the model's context ({context} tokens) is too short to read a "
                             "paper: give the reader service a longer context_length")  # fmt: skip
        body = text[:budget]
        from . import structured  # here: it reads this module's choices

        result = await self.router.complete({
            "model": model, "temperature": 0, "max_tokens": CARD_TOKENS,
            "response_format": structured.response_format("paper_card", structured.CARD),
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": head + body},
            ],
        })  # fmt: skip
        choice = (result["body"].get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        try:
            card = parse_card(content)
        except PaperError:
            used = _tokens_used(result["body"])
            # The context filled up (the engine cut the prompt, or the answer): say so,
            # not "not valid JSON", which reads as a model that can't follow orders.
            filled = bool(context) and used >= (context or 0) - CONTEXT_MARGIN // 4
            if filled or choice.get("finish_reason") == "length":
                size = f" ({context or used} tokens)" if context or used else ""
                raise PaperError(f"the paper didn't fit the model's context{size}: give the "
                                 "reader service a longer context_length") from None  # fmt: skip
            raise
        provenance = {**result["provenance"], "characters_read": len(body),
                      "at": now()}  # fmt: skip
        if context:
            provenance["context_length"] = context
        return card, provenance


def proposal(item: dict[str, Any], host_id: str, backend: str, baseline: str,
             initial_condition: str) -> dict[str, Any]:  # fmt: skip
    """The experiment a card suggests: its scheme (as a document) against a baseline,
    in a grid-refinement study: order, conservation, TVD and stability get checked."""
    data = item["data"]
    doc = data.get("scheme_ir")
    if item["state"] not in ("carded", "reported") or doc is None:
        raise PaperError(
            "only a carded paper whose method maps onto Newton's IR can propose an experiment"
            + (f" ({data.get('scheme_note')})" if data.get("scheme_note") else "")
        )
    common = {"resolutions": [64, 128, 256, 512, 1024], "initial_condition": initial_condition,
              "cfl": min(0.8, float(doc["claims"]["max_cfl"]))}  # fmt: skip
    title = (data.get("paper") or {}).get("title") or item["title"]
    claims = "; ".join(c["text"] for c in (data.get("card") or {}).get("claims", [])[:3])
    return {
        "title": f"{doc['name']} vs {baseline} (from arXiv:{item['external_id']})"[:200],
        "host_id": host_id,
        "backend": backend,
        "goal_id": item["goal_id"],
        "research_item_id": item["id"],
        "hypothesis": f"{title}: {claims}"[:2000] if claims else title[:2000],
        "variants": [
            {"role": "baseline", "label": baseline, "params": {"scheme": baseline, **common}},
            {
                "role": "candidate",
                "label": doc["name"],
                "params": {"scheme": "ir", "scheme_ir": doc, **common},
            },
        ],
    }
