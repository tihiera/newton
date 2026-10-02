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
    the user proposes (a card, or a reported paper again): the same memory is checked,
      unless they ask for a retest
    a plan whose experiment ended unreported (rejected, cancelled, failed) is over: its
      item goes back to carded (or reported, when it has findings) and can propose again

arXiv asks for at most one API request every 3 seconds: the loop keeps to that, and
reads at most MAX_NEW papers per goal per poll.

Offline (B7): a search that can't reach arXiv leaves last_polled_at alone; the goal gets
last_poll_error (a sentence) and next_poll_at (5 min, doubling to 1 h while it fails),
and one 'poll' event per failure streak. While Newton is offline the loop doesn't poll:
it probes arXiv at most once a minute, and polls the goals that failed once it's back.
Every poll that couldn't search (no model, no keywords, arXiv out of reach or answering
badly) is such a failure: only a search that worked sets last_polled_at. A goal that
failed for want of a model polls on the next pass once the profile has one.
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
from ..errors import Conflict, NotFound
from ..network import NetworkState
from ..orchestration.state_machine import RESEARCH_ITEM, ConcurrentTransition, record_event
from ..runners.base import RunnerError
from ..storage.db import Database, dumps, loads, new_id, now
from . import keywords as keywords_mod
from .papers import ARXIV_API, ArxivUnreachable, PaperError, Papers, parse_feed, proposal

log = logging.getLogger("newton_agentd.loop")

MAX_NEW = 8  # papers triaged per goal per poll
ARXIV_SPACING = 3.0  # seconds between arXiv API requests
FIRST_RETRY = 300.0  # a failed poll looks again in 5 min, doubling ...
LAST_RETRY = 3600.0  # ... up to 1 h, while it keeps failing
OFFLINE = ("arXiv couldn't be reached (offline?): Newton will look again when the network "
           "is back")  # fmt: skip
NO_MODEL = "no model: set default_model in the profile"
NO_KEYWORDS = "the goal has no keywords to search for: add some so Newton can look for papers"
TRIAGE = """You triage new papers for a research assistant that tests numerical methods on a
linear advection benchmark (1D/2D, periodic, finite volume). The user's goal:

  %(goal)s

Answer with ONE JSON object and nothing else:
{"relevant": true or false, "why": "one sentence"}

relevant: the paper proposes or analyses a finite-volume / finite-difference scheme (a flux,
a limiter, a time integrator) for advection or hyperbolic conservation laws that could be
tried on linear advection, and it serves the goal."""


def retry_delay(failures: int) -> float:
    """How long after the n-th failed poll in a row the loop looks again."""
    return min(LAST_RETRY, FIRST_RETRY * (1 << min(max(failures - 1, 0), 10)))


def failure_code(sentence: str | None) -> str | None:
    """The code of the failure a goal's last_poll_error says (None: no failure)."""
    if not sentence:
        return None
    return {NO_MODEL: "model", NO_KEYWORDS: "goal", OFFLINE: "offline"}.get(sentence, "arxiv")


def next_poll(goal: Any) -> float | None:
    """When the loop looks again for an active goal: the retry while polls fail, else
    poll_hours after the last poll that worked (None: never polled, the next pass)."""
    if goal["status"] != "active":
        return None
    if goal["next_poll_at"] is not None:
        return float(goal["next_poll_at"])
    if goal["last_polled_at"] is None:
        return None
    return float(goal["last_polled_at"]) + float(goal["poll_hours"]) * 3600


def search_query(keywords: list[str], categories: list[str]) -> str:
    """arXiv's search syntax from validated words (letters, digits, spaces, .+'-)."""
    words = [k for k in keywords if re.fullmatch(r"[A-Za-z0-9 .+'-]{2,60}", k)]
    cats = [c for c in categories if re.fullmatch(r"[a-z-]+(\.[A-Za-z-]+)?", c)]
    if not words:
        raise PaperError(NO_KEYWORDS)
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
        self._was_offline = False

    @property
    def network(self) -> NetworkState:
        return self.papers.network

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
                await self.poll_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("research loop pass failed")
            await asyncio.sleep(60)

    async def poll_due(self) -> None:
        """One pass of the loop: the due goals, unless Newton is offline (then a probe,
        at most once a minute). Back online: the goals that failed poll right away."""
        due = self.due_goals()
        if self.network.is_offline() and (due or self._was_offline):
            await self.network.probe("arxiv", self._probe)
        offline = self.network.is_offline()
        back, self._was_offline = self._was_offline and not offline, offline
        if back:
            failed = self.db.query("SELECT * FROM goals WHERE status = 'active' "
                                   "AND poll_failures > 0")  # fmt: skip
            due += [dict(g) for g in failed if all(d["id"] != g["id"] for d in due)]
        # Without a model a poll searches nothing: it isn't waiting for the network.
        searches = bool(self.profile.get()["default_model"])
        if searches:  # a model was chosen since: goals that waited for one look now
            waited = self.db.query("SELECT * FROM goals WHERE status = 'active' "
                                   "AND last_poll_error = ?", (NO_MODEL,))  # fmt: skip
            due += [dict(g) for g in waited if all(d["id"] != g["id"] for d in due)]
        for goal in due:
            if searches and self.network.is_offline():  # learned this pass: the rest wait
                self._waiting(goal)
                continue
            try:
                await self.poll(goal["id"])
            except (RunnerError, PaperError) as e:  # recorded on the goal: last_poll_error
                log.info("goal %s: %s", goal["id"], e)
            except asyncio.CancelledError:
                raise
            except Exception:  # one goal's failure never keeps the others from polling
                log.exception("goal %s: poll failed", goal["id"])

    async def _probe(self) -> None:
        """Any answer from arXiv's API (whatever its status) means it can be reached."""
        async with self.papers.http_factory() as http:
            await self._pace()
            await http.head(ARXIV_API)

    def due_goals(self) -> list[dict[str, Any]]:
        t = now()
        return [
            dict(g) for g in self.db.query("SELECT * FROM goals WHERE status = 'active'")
            if (when := next_poll(g)) is None or t >= when
        ]  # fmt: skip

    def _waiting(self, goal: dict[str, Any]) -> None:
        """A due goal not polled because Newton is offline: the same as a failed poll
        (error, next_poll_at), without trying arXiv."""
        self._failed(goal["id"], OFFLINE, "offline")

    def _failed(self, goal_id: str, sentence: str, code: str) -> RunnerError:
        """Record a poll that couldn't search arXiv: last_polled_at stays, the goal gets
        the sentence and its next try; one 'poll' event per failure streak. Another
        reason (offline, then no model) starts a new streak: its own event, 5 min."""
        with self.db.tx():
            row = self.db.query_one(
                "SELECT poll_failures, last_poll_error FROM goals WHERE id = ?", (goal_id,)
            )
            same = row is not None and failure_code(row["last_poll_error"]) == code
            failures = (row["poll_failures"] if row and same else 0) + 1
            self.db.execute(
                "UPDATE goals SET last_poll_error = ?, next_poll_at = ?, poll_failures = ? "
                "WHERE id = ?", (sentence, now() + retry_delay(failures), failures, goal_id),
            )  # fmt: skip
            if failures == 1:
                record_event(self.db, "goal", goal_id, "poll",
                             {"goal_id": goal_id, "error": sentence, "code": code})  # fmt: skip
        return RunnerError(sentence, code=code)

    async def _search(self, query: str) -> list[dict[str, Any]]:
        """arXiv's newest papers for the query. PaperError (a sentence) when it fails."""
        async with self.papers.http_factory() as http:
            await self._pace()
            params = httpx.QueryParams({
                "search_query": query, "sortBy": "submittedDate",
                "sortOrder": "descending", "max_results": 25,
            })  # fmt: skip
            resp = await self.papers.fetch(http, f"{ARXIV_API}?{params}")
            self._last_arxiv = time.monotonic()  # a retry inside fetch was a request too
        if resp.status_code != 200:
            raise PaperError(f"arXiv's API answered {resp.status_code}")
        return parse_feed(resp.content)  # a captive portal's page: PaperError

    # -- one poll ------------------------------------------------------------------------
    async def poll(self, goal_id: str) -> dict[str, Any]:
        """Search, triage, card, propose. Returns what happened (also as events)."""
        async with self._lock:
            self.record_findings()  # memory as of now: auto_propose checks it
            goal = self.db.query_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
            if goal is None:
                raise PaperError(f"unknown goal {goal_id}")
            model = self.profile.get()["default_model"]
            summary: dict[str, Any] = {"goal_id": goal_id, "found": 0, "new": 0, "relevant": 0,
                                       "dismissed": 0, "carded": 0, "proposed": [],
                                       "skipped": []}  # fmt: skip
            if not model:  # nothing searched: a failed poll (backs off; a model ends it)
                self._failed(goal_id, NO_MODEL, "model")
                summary["error"] = NO_MODEL
                return summary
            if not loads(goal["keywords"]):  # none yet: proposed from the topic, then saved
                found_kw, source = await keywords_mod.suggest(
                    self.router, model, goal["title"], goal["description"] or ""
                )
                if found_kw:
                    self.db.execute("UPDATE goals SET keywords = ?, updated_at = ? WHERE id = ?",
                                    (dumps(found_kw), now(), goal_id))  # fmt: skip
                    record_event(self.db, "goal", goal_id, "keywords",
                                 {"keywords": found_kw, "source": source})  # fmt: skip
                    goal = {**goal, "keywords": dumps(found_kw)}
            try:
                query = search_query(loads(goal["keywords"]), loads(goal["categories"]))
            except PaperError as e:  # the goal itself can't be searched: say so, back off
                self._failed(goal_id, str(e), "goal")
                raise
            try:
                found = await self._search(query)
            except ArxivUnreachable as e:  # no answer: offline (or arXiv is)
                if e.offline or self.network.is_offline():
                    raise self._failed(goal_id, OFFLINE, "offline") from None
                raise self._failed(goal_id, self._later(str(e), goal), "arxiv") from None
            except PaperError as e:  # an answer, not a search result (arXiv busy, a portal)
                raise self._failed(goal_id, self._later(str(e), goal), "arxiv") from None
            except Exception as e:  # noqa: BLE001 - e.g. redirects without end, a bad body
                log.warning("goal %s: arXiv search failed: %r", goal_id, e)
                reason = f"arXiv's answer couldn't be read ({type(e).__name__})"
                raise self._failed(goal_id, self._later(reason, goal), "arxiv") from None
            self.db.execute(
                "UPDATE goals SET last_polled_at = ?, last_poll_error = NULL, "
                "next_poll_at = NULL, poll_failures = 0 WHERE id = ?", (now(), goal_id),
            )  # fmt: skip
            summary["found"] = len(found)
            fresh = [p for p in found if not self.db.query_one(
                "SELECT 1 FROM research_items WHERE source = 'arxiv' AND external_id = ?",
                (p["arxiv_id"],))][:MAX_NEW]  # fmt: skip
            summary["new"] = len(fresh)
            for paper in fresh:
                await self._one(goal, paper, model, summary)
            record_event(self.db, "goal", goal_id, "poll", summary)
            return summary

    @staticmethod
    def _later(reason: str, goal: Any) -> str:
        """An "arxiv" failure's sentence, with when _failed will have the loop look again."""
        same = failure_code(goal["last_poll_error"]) == "arxiv"
        minutes = round(retry_delay((goal["poll_failures"] if same else 0) + 1) / 60)
        return f"{reason}: Newton will look again in {minutes} min"

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
        seen = self.memory(digest, item_id)
        return str(seen) if seen else None

    def memory(self, digest: str, item_id: str) -> Conflict | None:
        """already_tested(), with what it's about: a code and the experiment or item."""
        # A run that showed nothing (evidence "unknown": e.g. the baseline didn't run)
        # didn't test the scheme: it doesn't block testing it.
        found = self.db.query_one(
            "SELECT experiment_id, evidence FROM findings WHERE scheme_digest = ? "
            "AND evidence != 'unknown' ORDER BY created_at DESC LIMIT 1", (digest,),
        )  # fmt: skip
        if found:
            return Conflict(
                f"the same scheme was tested in {found['experiment_id']} ({found['evidence']})",
                code="already_tested", experiment_id=found["experiment_id"],
            )  # fmt: skip
        for row in self.db.query(
            "SELECT id, data FROM research_items WHERE id != ? AND state IN "
            "('experiment_planned', 'executing', 'evaluating')", (item_id,),
        ):  # fmt: skip
            if (loads(row["data"]) or {}).get("method_digest") == digest:
                return Conflict(f"the same scheme is already planned (from {row['id']})",
                                code="already_planned", research_item_id=row["id"])  # fmt: skip
        return None

    def propose(self, item_id: str, host_id: str, backend: str, baseline: str,
                initial_condition: str, retest: bool = False) -> dict[str, Any]:  # fmt: skip
        """The user's proposal: a card's experiment, or a reported paper's next one,
        created for approval. Unless it's a retest, a scheme already tested (the
        paper's own finding included) or already planned is refused (Conflict)."""
        self.record_findings()  # an experiment that just reported or ended counts now
        try:
            item = self.papers.get(item_id)
        except KeyError:
            raise NotFound(f"no research item {item_id}") from None
        spec = ExperimentSpec.model_validate(
            proposal(item, host_id, backend, baseline, initial_condition)
        )
        digest = item["data"].get("method_digest")
        seen = self.memory(digest, item_id) if digest and not retest else None
        if seen is not None:
            raise seen
        experiment: dict[str, Any] = self.experiments.create(spec)
        RESEARCH_ITEM.transition(self.db, item_id, item["state"], "experiment_planned",
                                 {"updated_at": now()})  # fmt: skip
        record_event(self.db, "research_item", item_id, "proposed",
                     {"experiment_id": experiment["id"], "retest": retest})  # fmt: skip
        return experiment

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
        self.settle_plans()
        return len(rows)

    def settle_plans(self) -> int:
        """Plans whose newest experiment ended without a report (rejected, cancelled,
        failed) are over: the item goes back to reported when it has findings, to carded
        otherwise. So it can propose again, and isn't "already planned" for anyone."""
        rows = self.db.query(
            "SELECT i.id, e.id AS experiment_id, e.state AS experiment_state, EXISTS ("
            "  SELECT 1 FROM findings f WHERE f.research_item_id = i.id) AS tested "
            "FROM research_items i JOIN experiments e ON e.id = ("
            "  SELECT x.id FROM experiments x WHERE x.research_item_id = i.id "
            "  ORDER BY x.created_at DESC, x.rowid DESC LIMIT 1) "
            "WHERE i.state = 'experiment_planned' "
            "AND e.state IN ('rejected', 'cancelled', 'failed')"
        )  # fmt: skip
        for row in rows:
            with contextlib.suppress(ConcurrentTransition):
                RESEARCH_ITEM.transition(
                    self.db, row["id"], "experiment_planned",
                    "reported" if row["tested"] else "carded", {"updated_at": now()},
                    {"experiment_id": row["experiment_id"],
                     "experiment_state": row["experiment_state"]},
                )  # fmt: skip
        return len(rows)

    def findings(self, goal_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM findings WHERE (? IS NULL OR goal_id = ?) ORDER BY created_at DESC",
            (goal_id, goal_id),
        )
        return [{**dict(r), "claims": loads(r["claims"])} for r in rows]
