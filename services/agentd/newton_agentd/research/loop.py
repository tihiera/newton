"""The research loop (B5): goals watch arXiv; new papers are triaged, carded and,
when their method is new, proposed as experiments; what experiments show is kept.

    every goal, every poll_hours: search arXiv (its keywords, in its categories)
      -> new papers only (dedup by arXiv id, across goals)
      -> triage: a model reads title + abstract: relevant to the goal? (cheap)
      -> relevant: full text, card, SchemeIR (B4); not: dismissed, with the reason
      -> a scheme not tested before (dedup by digest) and the goal's auto_propose:
         an experiment, created for approval: nothing runs without the user
    every finished experiment that came from a paper -> a finding (scientific memory):
      the scheme, its evidence, claim by claim; the paper's item -> reported

arXiv asks for at most one API request every 3 seconds: the loop keeps to that, and
reads at most MAX_NEW papers per goal per poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from typing import Any

import httpx

from ..config import Settings
from ..contracts import ExperimentSpec
from ..orchestration.state_machine import RESEARCH_ITEM, ConcurrentTransition, record_event
from ..storage.db import Database, dumps, loads, new_id, now
from .papers import ARXIV_API, PaperError, Papers, _get, parse_feed, proposal

log = logging.getLogger("newton_agentd.loop")

MAX_NEW = 8  # papers triaged per goal per poll
ARXIV_SPACING = 3.0  # seconds between arXiv API requests
TRIAGE = """You triage new papers for a research assistant that tests numerical methods on a
linear advection benchmark (1D/2D, periodic, finite volume). The user's goal:

  %(goal)s

Answer with ONE JSON object and nothing else:
{"relevant": true or false, "why": "one sentence"}

relevant: the paper proposes or analyses a finite-volume / finite-difference scheme (a flux,
a limiter, a time integrator) for advection or hyperbolic conservation laws that could be
tried on linear advection, and it serves the goal."""


def search_query(keywords: list[str], categories: list[str]) -> str:
    """arXiv's search syntax from validated words (letters, digits, spaces, .+'-)."""
    words = [k for k in keywords if re.fullmatch(r"[A-Za-z0-9 .+'-]{2,60}", k)]
    cats = [c for c in categories if re.fullmatch(r"[a-z-]+(\.[A-Za-z-]+)?", c)]
    if not words:
        raise PaperError("the goal has no keywords to search for")
    terms = " OR ".join(f'abs:"{w}"' if " " in w else f"abs:{w}" for w in words)
    query = f"({terms})"
    if cats:
        query += " AND (" + " OR ".join(f"cat:{c}" for c in cats) + ")"
    return query


class ResearchLoop:
    def __init__(self, settings: Settings, db: Database, papers: Papers, experiments: Any,
                 router: Any, profile: Any) -> None:  # fmt: skip
        self.settings = settings
        self.db = db
        self.papers = papers
        self.experiments = experiments
        self.router = router
        self.profile = profile
        self._task: asyncio.Task[None] | None = None
        self._last_arxiv = 0.0
        self._lock = asyncio.Lock()  # one poll at a time: arXiv's pace, one model

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="newton-research-loop")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                self.record_findings()
                for goal in self.due_goals():
                    await self.poll(goal["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("research loop pass failed")
            await asyncio.sleep(60)

    def due_goals(self) -> list[dict[str, Any]]:
        t = now()
        return [
            dict(g) for g in self.db.query("SELECT * FROM goals WHERE status = 'active'")
            if g["last_polled_at"] is None or t - g["last_polled_at"] >= g["poll_hours"] * 3600
        ]  # fmt: skip

    # -- one poll ------------------------------------------------------------------------
    async def poll(self, goal_id: str) -> dict[str, Any]:
        """Search, triage, card, propose. Returns what happened (also as events)."""
        async with self._lock:
            goal = self.db.query_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
            if goal is None:
                raise PaperError(f"unknown goal {goal_id}")
            self.db.execute("UPDATE goals SET last_polled_at = ? WHERE id = ?", (now(), goal_id))
            model = self.profile.get()["default_model"]
            summary: dict[str, Any] = {"goal_id": goal_id, "found": 0, "new": 0, "relevant": 0,
                                       "dismissed": 0, "carded": 0, "proposed": [],
                                       "skipped": []}  # fmt: skip
            if not model:
                summary["error"] = "no model: set default_model in the profile"
                record_event(self.db, "goal", goal_id, "poll", summary)
                return summary
            query = search_query(loads(goal["keywords"]), loads(goal["categories"]))
            async with self.papers.http_factory() as http:
                await self._pace()
                params = httpx.QueryParams({
                    "search_query": query, "sortBy": "submittedDate",
                    "sortOrder": "descending", "max_results": 25,
                })  # fmt: skip
                resp = await _get(http, f"{ARXIV_API}?{params}")
                if resp.status_code != 200:
                    raise PaperError(f"arXiv's API answered {resp.status_code}")
                found = parse_feed(resp.content)
            summary["found"] = len(found)
            fresh = [p for p in found if not self.db.query_one(
                "SELECT 1 FROM research_items WHERE source = 'arxiv' AND external_id = ?",
                (p["arxiv_id"],))][:MAX_NEW]  # fmt: skip
            summary["new"] = len(fresh)
            for paper in fresh:
                await self._one(goal, paper, model, summary)
            record_event(self.db, "goal", goal_id, "poll", summary)
            return summary

    async def _pace(self) -> None:
        wait = self._last_arxiv + ARXIV_SPACING - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_arxiv = time.monotonic()

    async def _one(self, goal: Any, paper: dict[str, Any], model: str,
                   summary: dict[str, Any]) -> None:  # fmt: skip
        t = now()
        item_id = new_id("paper")
        self.db.insert("research_items", {
            "id": item_id, "goal_id": goal["id"], "kind": "paper", "title": paper["title"][:300],
            "source": "arxiv", "external_id": paper["arxiv_id"], "state": "discovered",
            "data": dumps({"paper": paper, "model": model, "found_by": "research_loop"}),
            "created_at": t, "updated_at": t,
        })  # fmt: skip
        try:
            relevant, why = await self.triage(goal, paper, model)
        except Exception as e:  # can't triage now: leave it discovered, say why
            self.papers._update(item_id, error=f"triage: {e}"[:500])
            summary["skipped"].append({"item": item_id, "why": str(e)[:200]})
            return
        self.papers._update(item_id, triage={"relevant": relevant, "why": why, "model": model})
        if not relevant:
            self._move(item_id, "discovered", "dismissed")
            summary["dismissed"] += 1
            return
        summary["relevant"] += 1
        self._move(item_id, "discovered", "triaged")
        # Papers are released now and then: no need to wait for the full text at
        # arXiv's API pace (the HTML / PDF pages aren't the API), but stay polite.
        await self._pace()
        await self.papers.process(item_id, paper["arxiv_id"], model, paper)
        item = self.papers.get(item_id)
        if item["state"] != "carded":
            return
        summary["carded"] += 1
        if not goal["auto_propose"] or not item["data"].get("scheme_ir"):
            return
        tested = self.already_tested(item["data"]["method_digest"], item_id)
        if tested:
            self.papers._update(item_id, proposal_note=f"not proposed: {tested}")
            summary["skipped"].append({"item": item_id, "why": tested})
            return
        try:
            spec = ExperimentSpec.model_validate(proposal(item, "auto", "auto", "upwind", "sine"))
            experiment = self.experiments.create(spec)
        except ValueError as e:  # e.g. no host can run it now: keep the card, say why
            self.papers._update(item_id, proposal_note=f"not proposed: {e}"[:500])
            summary["skipped"].append({"item": item_id, "why": str(e)[:200]})
            return
        self._move(item_id, "carded", "experiment_planned")
        record_event(self.db, "research_item", item_id, "proposed",
                     {"experiment_id": experiment["id"], "by": "research_loop"})  # fmt: skip
        summary["proposed"].append(experiment["id"])

    async def triage(self, goal: Any, paper: dict[str, Any], model: str) -> tuple[bool, str]:
        goal_text = f"{goal['title']}. {goal['description']}".strip()[:1500]
        result = await self.router.complete({
            "model": model, "temperature": 0, "max_tokens": 200,
            "messages": [
                {"role": "system", "content": TRIAGE % {"goal": goal_text}},
                {"role": "user", "content": f"Title: {paper['title']}\n\nAbstract: "
                                            f"{paper['abstract'][:4000]}"},
            ],
        })  # fmt: skip
        content = ((result["body"].get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        ) or ""
        match = re.search(r"\{.*\}", content, re.S)
        try:
            answer = json.loads(match.group(0)) if match else {}
        except ValueError:
            answer = {}
        if not isinstance(answer, dict) or not isinstance(answer.get("relevant"), bool):
            raise PaperError("the model's triage answer was not {relevant, why}")
        return answer["relevant"], str(answer.get("why") or "")[:300]

    def already_tested(self, digest: str, item_id: str) -> str | None:
        """Scientific memory: this method (flux and time stepping, whatever its name)
        was tested, or is about to be."""
        found = self.db.query_one(
            "SELECT experiment_id, evidence FROM findings WHERE scheme_digest = ? "
            "ORDER BY created_at DESC LIMIT 1", (digest,),
        )  # fmt: skip
        if found:
            return f"the same scheme was tested in {found['experiment_id']} ({found['evidence']})"
        for row in self.db.query(
            "SELECT id, data FROM research_items WHERE id != ? AND state IN "
            "('experiment_planned', 'executing', 'evaluating')", (item_id,),
        ):  # fmt: skip
            if (loads(row["data"]) or {}).get("method_digest") == digest:
                return f"the same scheme is already planned (from {row['id']})"
        return None

    def _move(self, item_id: str, src: str, dst: str) -> None:
        with contextlib.suppress(ConcurrentTransition):
            RESEARCH_ITEM.transition(self.db, item_id, src, dst, {"updated_at": now()})

    # -- scientific memory ----------------------------------------------------------------
    def record_findings(self) -> int:
        """Each finished experiment that came from a paper, once: what it showed."""
        rows = self.db.query(
            "SELECT e.* FROM experiments e LEFT JOIN findings f ON f.experiment_id = e.id "
            "WHERE e.state = 'reported' AND e.research_item_id IS NOT NULL AND f.id IS NULL"
        )
        for exp in rows:
            evaluation = loads(exp["evaluation"]) or {}
            verdict = (evaluation.get("verdicts") or [{}])[0]
            candidate: dict[str, Any] = next((v for v in evaluation.get("variants") or []
                              if v.get("role") == "candidate"), {})  # fmt: skip
            spec = loads(exp["spec"])
            doc = next((v["params"].get("scheme_ir") for v in spec["variants"]
                        if v["role"] == "candidate"), None) or {}  # fmt: skip
            item = self.db.query_one("SELECT * FROM research_items WHERE id = ?",
                                     (exp["research_item_id"],))  # fmt: skip
            digest = (loads(item["data"]) or {}).get("method_digest") if item else None
            claims = [{"claim": a.get("claim"), "claimed": a.get("claimed"),
                       "holds": a.get("holds")}
                      for a in candidate.get("assumptions") or []]  # fmt: skip
            self.db.insert("findings", {
                "id": new_id("finding"), "goal_id": exp["goal_id"],
                "research_item_id": exp["research_item_id"], "experiment_id": exp["id"],
                "scheme_name": doc.get("name"), "scheme_digest": digest,
                "evidence": verdict.get("evidence") or exp["evidence"] or "unknown",
                "claims": dumps(claims), "summary": verdict.get("summary") or exp["title"],
                "created_at": now(),
            })  # fmt: skip
            if item is not None:
                steps = (
                    ("experiment_planned", "executing"),
                    ("executing", "evaluating"),
                    ("evaluating", "reported"),
                )
                for src, dst in steps:
                    current = self.db.query_one("SELECT state FROM research_items WHERE id = ?",
                                                (item["id"],))  # fmt: skip
                    if current and current["state"] == src:
                        self._move(item["id"], src, dst)
            record_event(self.db, "experiment", exp["id"], "finding",
                         {"evidence": verdict.get("evidence")})  # fmt: skip
        return len(rows)

    def findings(self, goal_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM findings WHERE (? IS NULL OR goal_id = ?) ORDER BY created_at DESC",
            (goal_id, goal_id),
        )
        return [{**dict(r), "claims": loads(r["claims"])} for r in rows]
