"""Publishing reports to GitHub and Notion (B6). Every publication is approved first.

    request -> the report's text is frozen (on disk + sha256) and an approval shows
    where it goes, what it is, and a preview -> approved: exactly that text is sent
    -> published (with its URL) or failed (with why); rejected: nothing leaves the Mac.

GitHub: a secret gist (report.md), or an issue in a repository. Notion: a page under
a parent page the user's integration can see. Tokens live in the secret store
(Keychain): from one-click Connect (oauth.py), pasted, or GitHub's imported from the
GitHub CLI (`gh auth token`, a fixed argv) when the user asks. Notion's OAuth token is
refreshed before it expires (NotionSession).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..errors import Conflict
from ..orchestration.approvals import Approvals
from ..orchestration.state_machine import record_event
from ..runners.base import RunnerError
from ..secrets import SecretStore
from ..storage.db import Database, Row, dumps, loads, new_id, now
from .oauth import NOTION_REFRESH, TOKENS, NotionReauth, NotionSession, OAuth

log = logging.getLogger("newton_agentd.publish")

APPROVAL_KIND = "publish_report"
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
NOTION_ID = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}")
ISSUE_LIMIT = 65_000  # GitHub's issue body limit is 65,536 characters
# Connect asks GitHub for public_repo only: a private repository answers 404.
PRIVATE_REPO = ("GitHub couldn't find {repo}: Connect reaches public repositories only; "
                "paste a token with repo access for private ones")  # fmt: skip


class PublishError(ValueError):
    pass


class Publisher:
    def __init__(self, settings: Settings, db: Database, approvals: Approvals,
                 secrets: SecretStore) -> None:  # fmt: skip
        self.settings = settings
        self.db = db
        self.secrets = secrets
        approvals.register(APPROVAL_KIND, self._on_decision)
        self.approvals = approvals
        self._tasks: set[asyncio.Task[None]] = set()
        self.http_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15)
        )
        # One-click Connect shares the HTTP factory (tests mock both through it).
        self.oauth = OAuth(settings, db, secrets, lambda: self.http_factory())

    @property
    def dir(self) -> Path:
        return self.settings.data_dir / "publications"

    # -- connections ----------------------------------------------------------------------
    def connections(self) -> dict[str, Any]:
        """{github, notion: connected?, accounts: who (or null), oauth: Connect set up?}"""
        connected = {target: bool(self.secrets.get(ref)) for target, ref in TOKENS.items()}
        accounts = {t: self.oauth.accounts.get(t) if ok else None for t, ok in connected.items()}
        return {**connected, "accounts": accounts, "oauth": self.oauth.configured()}

    def connect(self, target: str, token: str, method: str = "token") -> dict[str, Any]:
        if target not in TOKENS:
            raise PublishError(f"unknown target {target}")
        if not re.fullmatch(r"[A-Za-z0-9_\-.]{20,300}", token):
            raise PublishError("that doesn't look like an API token")
        # Notion: a refresh still waiting for the broker must not write over the paste.
        with self.oauth.tokens_change(target):
            self.secrets.set(TOKENS[target], token)
            if target == "notion":  # a pasted integration token doesn't expire or refresh
                with contextlib.suppress(Exception):
                    self.secrets.delete(NOTION_REFRESH)
            # Who it is stays unknown (""): looking it up would slow the paste down.
            self.oauth.accounts.save(target, name="", icon=None, method=method)
        return self.connections()

    def disconnect(self, target: str) -> dict[str, Any]:
        """Forget the token (and Notion's refresh token) and who was connected. A GitHub
        OAuth grant can only be revoked on github.com (that needs the client secret)."""
        if target in TOKENS:
            refs = (TOKENS[target], NOTION_REFRESH) if target == "notion" else (TOKENS[target],)
            # Notion: a refresh still waiting for the broker must not bring the tokens back.
            with self.oauth.tokens_change(target):
                for ref in refs:
                    with contextlib.suppress(Exception):
                        self.secrets.delete(ref)
                self.oauth.accounts.delete(target)
        return self.connections()

    def import_gh_token(self) -> dict[str, Any]:
        """The GitHub CLI's token, on the user's request (fixed argv, no shell)."""
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                 timeout=15, check=True)  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            raise PublishError(f"the GitHub CLI has no token to give: {e}") from None
        return self.connect("github", out.stdout.strip(), method="gh")

    # -- Notion pages (to pick a parent page) -----------------------------------------------
    async def notion_pages(self, query: str = "") -> list[dict[str, Any]]:
        """The pages the user's integration can see, last edited first."""
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"}, "page_size": 50,
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }  # fmt: skip
        if query.strip():
            body["query"] = query.strip()
        try:
            async with self.http_factory() as http:
                notion = NotionSession(self.oauth, http)
                if not await notion.open():  # the secret store may block: off the loop
                    raise Conflict("Notion isn't connected", code="not_connected")
                resp = await notion.request("POST", f"{self.settings.notion_api}/search", body)
        except NotionReauth as e:
            raise Conflict(str(e), code="notion_reauth") from None
        except httpx.HTTPError as e:
            raise RunnerError(_safe(f"Notion couldn't be reached: {e}"), code="notion") from None
        if resp.status_code != 200:
            raise RunnerError(_safe(f"Notion answered {resp.status_code}: {_api_message(resp)}"),
                              transient=False, code="notion")  # fmt: skip
        try:
            data = resp.json()
        except ValueError:  # an HTML page from a proxy or captive portal, an empty body
            data = None
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise RunnerError("Notion answered 200 with something that isn't a search result",
                              transient=False, code="notion")  # fmt: skip
        pages = [notion_page(p) for p in results if isinstance(p, dict)]
        return [p for p in pages if p is not None]

    # -- requests ------------------------------------------------------------------------
    def request(
        self, experiment_id: str, target: str, destination: dict[str, Any]
    ) -> dict[str, Any]:
        exp = self.db.query_one("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
        if exp is None:
            raise KeyError(experiment_id)
        if exp["state"] != "reported" or not exp["report_path"]:
            raise PublishError("only a reported experiment has a report to publish")
        if target not in TOKENS:
            raise PublishError("target is github or notion")
        if not self.secrets.get(TOKENS[target]):
            raise PublishError(f"not connected to {target}: add a token first")
        destination = self._check_destination(target, destination)
        text = Path(exp["report_path"]).read_text(encoding="utf-8")
        text = f"{text}\n\n---\n_Published by Newton from experiment `{experiment_id}`._\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        pub_id = new_id("pub")
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{pub_id}.md"
        path.write_text(text, encoding="utf-8")  # what is approved is what is sent
        t = now()
        where = ("a secret GitHub gist" if destination.get("kind") == "gist"
                 else f"an issue in {destination.get('repo')}" if target == "github"
                 else f"a Notion page under {destination['parent_page_id']}")  # fmt: skip
        with self.db.tx():
            self.db.insert("publications", {
                "id": pub_id, "experiment_id": experiment_id, "target": target,
                "destination": dumps(destination), "state": "awaiting_approval",
                "content_sha256": digest, "content_path": str(path),
                "created_at": t, "updated_at": t,
            })  # fmt: skip
            self.approvals.request(
                APPROVAL_KIND, "publication", pub_id,
                f"Publish the report of “{exp['title']}” as {where}",
                {"target": target, "destination": destination, "experiment_id": experiment_id,
                 "evidence": exp["evidence"], "characters": len(text), "sha256": digest,
                 "preview": text[:1500]},
            )  # fmt: skip
        return self.get(pub_id)

    @staticmethod
    def _check_destination(target: str, d: dict[str, Any]) -> dict[str, Any]:
        if target == "github":
            kind = d.get("kind", "gist")
            if kind == "gist":
                return {"kind": "gist"}
            if kind == "issue" and isinstance(d.get("repo"), str) and REPO.fullmatch(d["repo"]):
                return {"kind": "issue", "repo": d["repo"]}
            raise PublishError("github: {kind: gist} or {kind: issue, repo: owner/name}")
        page = str(d.get("parent_page_id") or "").strip()
        if not NOTION_ID.fullmatch(page):
            raise PublishError("notion: {parent_page_id: the page's id (32 hex characters)}")
        return {"parent_page_id": page}

    def get(self, pub_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM publications WHERE id = ?", (pub_id,))
        if row is None:
            raise KeyError(pub_id)
        return self.view(row)

    @staticmethod
    def view(row: Row) -> dict[str, Any]:
        out = dict(row)
        out["destination"] = loads(row["destination"])
        out.pop("content_path", None)
        return out

    def list(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM publications WHERE (? IS NULL OR experiment_id = ?) "
            "ORDER BY created_at DESC", (experiment_id, experiment_id),
        )  # fmt: skip
        return [self.view(r) for r in rows]

    def _set(self, pub_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(f"UPDATE publications SET {assignments} WHERE id = :_id",  # noqa: S608
                        {**fields, "_id": pub_id})  # fmt: skip

    def _on_decision(self, db: Database, approval: Row, approved: bool) -> None:
        pub_id = approval["subject_id"]
        if not approved:
            self._set(pub_id, state="rejected")
            return
        self._set(pub_id, state="approved")
        task = asyncio.get_running_loop().create_task(self._publish(pub_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        await self.oauth.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- sending -------------------------------------------------------------------------
    async def _publish(self, pub_id: str) -> None:
        row = self.db.query_one("SELECT * FROM publications WHERE id = ?", (pub_id,))
        if row is None or row["state"] != "approved":
            return
        self._set(pub_id, state="publishing")
        try:
            text = Path(row["content_path"]).read_text(encoding="utf-8")
            if hashlib.sha256(text.encode()).hexdigest() != row["content_sha256"]:
                raise PublishError("the report changed after it was approved: not sent")
            exp = self.db.query_one("SELECT title FROM experiments WHERE id = ?",
                                    (row["experiment_id"],))  # fmt: skip
            title = f"Newton: {exp['title'] if exp else row['experiment_id']}"[:200]
            destination = loads(row["destination"])
            async with self.http_factory() as http:
                if row["target"] == "github":
                    token = await asyncio.to_thread(self.secrets.get, TOKENS["github"])
                    if not token:
                        raise PublishError("not connected to github any more")
                    account = self.oauth.accounts.get("github")
                    method = account["method"] if account else None
                    url = await self._github(http, token, destination, title, text, method)
                else:
                    notion = NotionSession(self.oauth, http)
                    if not await notion.open():
                        raise PublishError("not connected to notion any more")
                    url = await self._notion(notion, destination, title, text)
            self._set(pub_id, state="published", url=url, error=None)
            record_event(self.db, "publication", pub_id, "published", {"url": url})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - any failure is the publication's result
            log.warning("publication %s failed: %s", pub_id, e)
            self._set(pub_id, state="failed", error=_safe(str(e))[:500])

    async def _github(self, http: httpx.AsyncClient, token: str, d: dict[str, Any],
                      title: str, text: str, method: str | None = None) -> str:  # fmt: skip
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}  # fmt: skip
        if d["kind"] == "gist":
            resp = await http.post(f"{self.settings.github_api}/gists", headers=headers, json={
                "description": title, "public": False,
                "files": {"newton-report.md": {"content": text}}})  # fmt: skip
        else:
            body = (
                text
                if len(text) <= ISSUE_LIMIT
                else (text[: ISSUE_LIMIT - 200] + "\n\n_(truncated: the full report is in Newton)_")
            )
            issues = f"{self.settings.github_api}/repos/{d['repo']}/issues"
            resp = await http.post(issues, headers=headers, json={"title": title, "body": body})
            if resp.status_code == 404 and method == "oauth":
                raise PublishError(PRIVATE_REPO.format(repo=d["repo"]))
        if resp.status_code not in (200, 201):
            raise PublishError(f"GitHub answered {resp.status_code}: {_api_message(resp)}")
        url: str = resp.json()["html_url"]
        return url

    async def _notion(self, notion: NotionSession, d: dict[str, Any],
                      title: str, text: str) -> str:  # fmt: skip
        api = self.settings.notion_api
        blocks = notion_blocks(text)
        resp = await notion.request("POST", f"{api}/pages", {
            "parent": {"page_id": d["parent_page_id"]},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": blocks[:100],
        })  # fmt: skip
        if resp.status_code != 200:
            raise PublishError(f"Notion answered {resp.status_code}: {_api_message(resp)}")
        page = resp.json()
        for start in range(100, len(blocks), 100):  # Notion takes 100 blocks per request
            batch = {"children": blocks[start : start + 100]}
            more = await notion.request("PATCH", f"{api}/blocks/{page['id']}/children", batch)
            if more.status_code != 200:
                raise PublishError(f"Notion answered {more.status_code}: {_api_message(more)}")
        url: str = page["url"]
        return url


def _api_message(resp: httpx.Response) -> str:
    with contextlib.suppress(ValueError, AttributeError):
        return str(resp.json().get("message") or "")[:200]
    return ""


def _safe(text: str) -> str:
    """Never a token in an error message."""
    return re.sub(r"(gh[pousr]_|github_pat_|secret_|ntn_)[A-Za-z0-9_]+", "<token>", text)


def notion_page(page: dict[str, Any]) -> dict[str, Any] | None:
    """A search result as {id (32 hex), title, url, icon (an emoji, or none)}."""
    page_id = str(page.get("id") or "").replace("-", "")
    if page.get("object", "page") != "page" or not re.fullmatch(r"[0-9a-fA-F]{32}", page_id):
        return None
    title = ""
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = "".join(str(t.get("plain_text") or "") for t in prop.get("title") or []
                            if isinstance(t, dict))  # fmt: skip
            break
    icon = page.get("icon")
    icon = icon if isinstance(icon, dict) else {}
    emoji = icon.get("emoji") if icon.get("type") == "emoji" else None
    url = page.get("url")
    return {"id": page_id.lower(), "title": title.strip() or "Untitled",
            "url": url if isinstance(url, str) else None,
            "icon": emoji if isinstance(emoji, str) else None}  # fmt: skip


def _rich(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[i:i + 2000]}}
            for i in range(0, max(1, len(text)), 2000)][:100]  # fmt: skip


def notion_blocks(markdown: str) -> list[dict[str, Any]]:
    """The report's markdown as Notion blocks: headings, lists, tables (as monospaced
    text, so columns stay aligned), images as links, the rest as paragraphs."""
    blocks: list[dict[str, Any]] = []
    table: list[str] = []

    def flush_table() -> None:
        if table:
            code = {"language": "markdown", "rich_text": _rich("\n".join(table))}
            blocks.append({"type": "code", "code": code})
            table.clear()

    for line in markdown.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        flush_table()
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,3}) (.*)$", line)
        if heading:
            kind = f"heading_{len(heading.group(1))}"
            blocks.append({"type": kind, kind: {"rich_text": _rich(heading.group(2))}})
        elif line.startswith(("- ", "* ")):
            blocks.append({"type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich(line[2:])}})  # fmt: skip
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": _rich(line)}})
    flush_table()
    return blocks
