"""One-click Connect for GitHub and Notion (pasting a token stays as the fallback).

GitHub: the device flow of an OAuth App (no client secret). agentd asks GitHub for a
code, the user enters it on github.com, and a background task polls GitHub until the
user approves, declines, or the code expires.

Notion: a public integration. agentd hands out Notion's authorize URL with a one-time
state (10 minutes, memory only); Notion sends the browser back to agentd's callback
with a code, which the broker the user deploys (services/notion-broker, the only place
that holds Notion's client secret) trades for tokens. Notion's access token expires:
it is refreshed through the broker shortly before, or once when Notion answers 401.
A refresh Notion rejects (400/401) marks the account needs_reauth until the user
connects again; a broker or Notion out of reach (or answering 429/5xx) is only a
moment's trouble and changes nothing.

Tokens live in the secret store only, under the refs below; who is connected (a name,
an icon, how, until when) is in connector_accounts. No token, code or state is ever
logged or returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import logging
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Settings
from ..errors import Conflict
from ..network import NetworkState
from ..orchestration.state_machine import record_event
from ..runners.base import RunnerError
from ..secrets import SecretStore
from ..storage.db import Database, now

log = logging.getLogger("newton_agentd.oauth")

TOKENS = {"github": "github-token", "notion": "notion-token"}
NOTION_REFRESH = "notion-refresh-token"
NOTION_VERSION = "2022-06-28"
GITHUB_SCOPE = "gist public_repo"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
STATE_TTL = 600.0  # a Notion sign-in link is good for 10 minutes
REFRESH_MARGIN = 60.0  # refresh Notion's token when it has less than this left

NOT_CONFIGURED = {
    "github": "GitHub sign-in isn't set up in this build: paste a token instead",
    "notion": "Notion sign-in isn't set up in this build: paste a token instead",
}
CODE_EXPIRED = "the code expired: start again"
GITHUB_DENIED = "you declined on GitHub"
LINK_EXPIRED = "the sign-in link expired: start again"
NOTION_DENIED = "you declined on Notion"
GITHUB_CANCELLED = "the GitHub sign-in was cancelled"
REAUTH = "Notion's sign-in expired: connect Notion again"
RENEW_LATER = "Notion couldn't be reached to renew the sign-in: try again in a moment"
PAGE_EXPIRED = "This sign-in link has expired: start again from Newton"
PAGE_CONNECTED = "Notion is connected. You can close this tab and go back to Newton."
PAGE_CANCELLED = (
    "This sign-in was cancelled in Newton: nothing was connected. You can close this tab."
)
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class NotionReauth(Exception):
    """Notion's token can't be refreshed: the user has to connect Notion again."""

    def __init__(self) -> None:
        super().__init__(REAUTH)


class Accounts:
    """Who is connected to each target, and how. Never a token."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, target: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM connector_accounts WHERE target = ?", (target,))
        if row is None:
            return None
        return {"name": row["name"] or "", "icon": row["icon"], "method": row["method"],
                "connected_at": row["connected_at"],
                "needs_reauth": bool(row["needs_reauth"])}  # fmt: skip

    def expires_at(self, target: str) -> float | None:
        row = self.db.query_one("SELECT expires_at FROM connector_accounts WHERE target = ?",
                                (target,))  # fmt: skip
        return None if row is None or row["expires_at"] is None else float(row["expires_at"])

    def save(self, target: str, *, name: str, icon: str | None, method: str,
             expires_at: float | None = None) -> None:  # fmt: skip
        """A new connection: replaces the last one, needs_reauth cleared."""
        self.db.execute(
            "INSERT OR REPLACE INTO connector_accounts "
            "(target, name, icon, method, expires_at, connected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (target, name[:200], icon[:2000] if icon else None, method, expires_at, now()),
        )

    def set_expiry(self, target: str, expires_at: float | None) -> None:
        self.db.execute("UPDATE connector_accounts SET expires_at = ? WHERE target = ?",
                        (expires_at, target))  # fmt: skip

    def set_needs_reauth(self, target: str) -> None:
        self.db.execute("UPDATE connector_accounts SET needs_reauth = 1 WHERE target = ?",
                        (target,))  # fmt: skip

    def delete(self, target: str) -> None:
        self.db.execute("DELETE FROM connector_accounts WHERE target = ?", (target,))


@dataclass
class Flow:
    """One sign-in in progress (or how the last one ended)."""

    state: str = "none"  # none, pending, connected, denied, expired, failed
    error: str | None = None
    user_code: str | None = None
    verification_uri: str | None = None
    expires_at: float | None = None
    connecting: bool = False  # the token is in: storing it is no longer cancellable


class OAuth:
    def __init__(self, settings: Settings, db: Database, secrets: SecretStore,
                 http: Callable[[], httpx.AsyncClient],
                 network: NetworkState | None = None) -> None:  # fmt: skip
        self.settings = settings
        self.db = db
        self.secrets = secrets
        self.http = http
        self.network = network or NetworkState()  # told how each call went
        self.accounts = Accounts(db)
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # tests drive the poll
        self.state_ttl = STATE_TTL
        self._github = Flow()
        self._github_task: asyncio.Task[None] | None = None
        self._github_start_lock = asyncio.Lock()  # one start at a time, task included
        self._github_gen = 0  # bumped by a cancel: a start in flight then starts nothing
        self._notion = Flow()
        self._notion_state: str | None = None  # the one pending state value, single use
        self._notion_storing: asyncio.Event | None = None  # set when a callback has stored
        self._refresh_lock = asyncio.Lock()
        # Held while Notion's stored tokens change (a paste, a disconnect, a sign-in, a
        # refresh writing back); each change but a refresh bumps the generation, so a
        # refresh answered after one keeps its tokens to itself.
        self._notion_lock = threading.Lock()
        self._notion_gen = 0

    def configured(self) -> dict[str, bool]:
        s = self.settings
        return {"github": bool(s.github_client_id),
                "notion": bool(s.notion_client_id and s.notion_broker_url)}  # fmt: skip

    def _status(self, target: str, flow: Flow) -> dict[str, Any]:
        return {
            "state": flow.state, "error": flow.error, "user_code": flow.user_code,
            "verification_uri": flow.verification_uri, "expires_at": flow.expires_at,
            "account": self.accounts.get(target) if flow.state == "connected" else None,
        }  # fmt: skip

    def _connected(self, target: str, flow: Flow) -> None:
        flow.state, flow.error = "connected", None
        flow.user_code = flow.verification_uri = None
        record_event(self.db, "connector", target, "connected", {"method": "oauth"})

    @staticmethod
    def _end(flow: Flow, state: str, error: str) -> None:
        flow.state, flow.error, flow.connecting = state, error, False
        flow.user_code = flow.verification_uri = None

    # -- GitHub: device flow --------------------------------------------------------------
    async def github_start(self) -> dict[str, Any]:
        if not self.settings.github_client_id:
            raise Conflict(NOT_CONFIGURED["github"], code="not_configured")
        gen = self._github_gen  # as the request arrived: a cancel after it wins
        # Starts queue up (a double click, two windows): each replaces the one before,
        # task included, so exactly one poller is ever running.
        async with self._github_start_lock:
            return await self._github_start(gen)

    async def _github_start(self, gen: int) -> dict[str, Any]:
        await self._github_stop()  # one sign-in at a time: a new one replaces the last
        self._github = Flow()
        try:
            async with self.http() as http:
                resp = await http.post(
                    f"{self.settings.github_web}/login/device/code",
                    headers={"Accept": "application/json"},
                    data={"client_id": self.settings.github_client_id, "scope": GITHUB_SCOPE},
                )
        except httpx.HTTPError as e:
            self.network.note_failure("github", e)
            raise RunnerError("GitHub couldn't be reached", code="github") from None
        self.network.note_ok("github")
        data = _json(resp)
        device_code, user_code = data.get("device_code"), data.get("user_code")
        uri, expires_in = data.get("verification_uri"), data.get("expires_in")
        if (resp.status_code != 200 or data.get("error") or not isinstance(device_code, str)
                or not isinstance(user_code, str) or not isinstance(uri, str)
                or not isinstance(expires_in, int | float)):  # fmt: skip
            raise RunnerError(f"GitHub answered {resp.status_code}: {_message(data)}",
                              transient=False, code="github")  # fmt: skip
        interval = data.get("interval")
        interval = float(interval) if isinstance(interval, int | float) else 5.0
        if gen != self._github_gen:  # cancelled while GitHub answered: start nothing
            raise Conflict(GITHUB_CANCELLED, code="cancelled")
        flow = Flow("pending", None, user_code, uri, time.time() + float(expires_in))
        self._github = flow
        self._github_task = asyncio.get_running_loop().create_task(
            self._github_poll(flow, device_code, interval)
        )
        return {"user_code": user_code, "verification_uri": uri,
                "expires_at": flow.expires_at, "interval": interval}  # fmt: skip

    def github_status(self) -> dict[str, Any]:
        return self._status("github", self._github)

    async def github_cancel(self) -> dict[str, Any]:
        """Stop a pending sign-in. Once GitHub has handed over the token it is too late:
        the connection is finished (stored, account saved, event recorded) and reported."""
        self._github_gen += 1
        await self._github_stop()
        if self._github.state != "connected":
            self._github = Flow()
        return self.github_status()

    async def _github_stop(self) -> None:
        task, self._github_task = self._github_task, None
        if task is None or task.done():
            return
        if self._github.connecting:  # never half a connection: let it finish
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _github_poll(self, flow: Flow, device_code: str, interval: float) -> None:
        try:
            while True:
                await self.sleep(interval)
                if time.time() >= (flow.expires_at or 0):
                    self._end(flow, "expired", CODE_EXPIRED)
                    return
                try:
                    async with self.http() as http:
                        resp = await http.post(
                            f"{self.settings.github_web}/login/oauth/access_token",
                            headers={"Accept": "application/json"},
                            data={"client_id": self.settings.github_client_id,
                                  "device_code": device_code, "grant_type": DEVICE_GRANT},
                        )  # fmt: skip
                except httpx.HTTPError as e:
                    self.network.note_failure("github", e)
                    continue  # GitHub out of reach for a moment: the code is still good
                self.network.note_ok("github")
                data = _json(resp)
                token = data.get("access_token")
                if isinstance(token, str) and token:
                    flow.connecting = True  # before any await: a cancel now waits for it
                    await self._github_connected(flow, token)
                    return
                error = data.get("error")
                if error == "authorization_pending" or (not error and resp.status_code >= 500):
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error == "expired_token":
                    self._end(flow, "expired", CODE_EXPIRED)
                elif error == "access_denied":
                    self._end(flow, "denied", GITHUB_DENIED)
                elif error:
                    self._end(flow, "failed", f"GitHub refused the sign-in: {_message(data)}")
                else:
                    self._end(flow, "failed", f"GitHub answered {resp.status_code}")
                log.info("GitHub sign-in ended: %s", flow.error)
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - whatever went wrong ends this sign-in
            self._end(flow, "failed", "GitHub sign-in failed: something went wrong in Newton")
            log.warning("GitHub sign-in failed (%s)", type(e).__name__)

    async def _github_connected(self, flow: Flow, token: str) -> None:
        await asyncio.to_thread(self.secrets.set, TOKENS["github"], token)
        login, avatar = "", None
        try:  # who it is: nice to show, not needed to publish
            async with self.http() as http:
                resp = await http.get(f"{self.settings.github_api}/user", headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"})  # fmt: skip
            self.network.note_ok("github")
            user = _json(resp) if resp.status_code == 200 else {}
            login, avatar = _str(user, "login") or "", _str(user, "avatar_url")
        except httpx.HTTPError as e:
            self.network.note_failure("github", e)
        self.accounts.save("github", name=login or "", icon=avatar, method="oauth")
        flow.connecting = False
        self._connected("github", flow)
        log.info("GitHub connected as %s", login or "an unknown user")

    # -- Notion: authorization code through the broker ------------------------------------
    def notion_start(self) -> dict[str, Any]:
        s = self.settings
        if not (s.notion_client_id and s.notion_broker_url):
            raise Conflict(NOT_CONFIGURED["notion"], code="not_configured")
        state = secrets.token_urlsafe(32)
        self._notion_state = state  # a new sign-in replaces the last: one pending at a time
        self._notion = Flow("pending", None, None, None, time.time() + self.state_ttl)
        query = urlencode({"client_id": s.notion_client_id, "response_type": "code",
                           "owner": "user", "redirect_uri": s.notion_callback_url,
                           "state": state})  # fmt: skip
        url = f"{s.notion_api}/oauth/authorize?{query}"
        # The status repeats it (verification_uri) for the UI to reopen; never logged.
        self._notion.verification_uri = url
        return {"url": url, "expires_at": self._notion.expires_at}

    def notion_status(self) -> dict[str, Any]:
        flow = self._notion
        if flow.state == "pending" and time.time() >= (flow.expires_at or 0):
            self._notion_state = None
            self._end(flow, "expired", LINK_EXPIRED)
        return self._status("notion", flow)

    async def notion_cancel(self) -> dict[str, Any]:
        """Stop a pending sign-in: its state is forgotten, so Notion's callback with it
        gets the expired page, and a token exchange still in flight is dropped. Once the
        broker's tokens are being stored it is too late: the connection is finished and
        reported (state connected)."""
        storing = self._notion_storing
        if self._notion.connecting and storing is not None:
            await storing.wait()
        self._notion_state = None
        if self._notion.state != "connected":
            self._notion = Flow()
        return self.notion_status()

    async def notion_callback(self, code: str | None, state: str | None,
                              error: str | None) -> tuple[int, str]:  # fmt: skip
        """Notion sent the browser back: (HTTP status, the sentence for the page)."""
        flow, pending = self._notion, self._notion_state
        if (pending is None or not state or flow.state != "pending"
                or not hmac.compare_digest(state.encode(), pending.encode())):  # fmt: skip
            return 400, PAGE_EXPIRED
        self._notion_state = None  # single use, whatever happens next
        if time.time() >= (flow.expires_at or 0):
            self._end(flow, "expired", LINK_EXPIRED)
            return 400, PAGE_EXPIRED
        if error == "access_denied":
            self._end(flow, "denied", NOTION_DENIED)
            return 200, "You declined on Notion: nothing was connected. You can close this tab."
        if error or not code:
            reason = (error or "no code")[:100]
            return self._notion_failed(flow, f"Notion refused the sign-in: {reason}")
        resp: httpx.Response | None = None
        try:
            async with self.http() as http:
                resp = await http.post(f"{self._broker()}/notion/token", json={
                    "code": code, "redirect_uri": self.settings.notion_callback_url})  # fmt: skip
            self.network.note_ok("notion")
        except httpx.HTTPError as e:
            self.network.note_failure("notion", e)
        if self._notion is not flow or flow.state != "pending":
            log.info("Notion sign-in was cancelled or replaced meanwhile: its answer was dropped")
            return 400, PAGE_CANCELLED
        if resp is None:
            return self._notion_failed(flow, "Newton's Notion sign-in service couldn't be reached")
        data = _json(resp)
        if resp.status_code != 200:
            answer = f"Notion answered {resp.status_code}: {_message(data)}"
            return self._notion_failed(flow, answer)
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            return self._notion_failed(flow, "Notion answered without a token")
        # From here the tokens are in: a cancel waits for the connection to be whole.
        flow.connecting = True
        storing = self._notion_storing = asyncio.Event()
        try:
            await self._store_notion(data, access)
            name, icon = _str(data, "workspace_name") or "", _str(data, "workspace_icon")
            self.accounts.save("notion", name=name, icon=icon, method="oauth",
                               expires_at=_expiry(data))  # fmt: skip
            flow.connecting = False
            self._connected("notion", flow)
        except Exception:
            self._end(flow, "failed", "Notion sign-in failed: something went wrong in Newton")
            raise
        finally:
            storing.set()
        log.info("Notion connected to the workspace %s", name or "(unnamed)")
        return 200, PAGE_CONNECTED

    def _notion_failed(self, flow: Flow, error: str) -> tuple[int, str]:
        self._end(flow, "failed", error)
        log.info("Notion sign-in failed: %s", error)
        return 502, f"Notion couldn't be connected: {error}. Go back to Newton and try again."

    def _broker(self) -> str:
        return self.settings.notion_broker_url.rstrip("/")

    @contextlib.contextmanager
    def tokens_change(self, target: str) -> Iterator[None]:
        """Around any change to a target's stored tokens but a refresh's own write. For
        Notion, a refresh still waiting for the broker then drops what it gets back."""
        if target != "notion":
            yield
            return
        with self._notion_lock:
            self._notion_gen += 1
            yield

    def _write_notion(self, data: dict[str, Any], access: str) -> None:
        self.secrets.set(TOKENS["notion"], access)
        refresh = data.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            self.secrets.set(NOTION_REFRESH, refresh)

    async def _store_notion(self, data: dict[str, Any], access: str) -> None:
        def store() -> None:
            with self.tokens_change("notion"):
                self._write_notion(data, access)

        await asyncio.to_thread(store)

    def _keep_refreshed(self, gen: int, current: str | None, data: dict[str, Any],
                        access: str, expires_at: float | None) -> bool:  # fmt: skip
        """Store a refresh's tokens unless Notion was disconnected, pasted or signed in
        again meanwhile (False: the tokens are dropped)."""
        with self._notion_lock:
            if gen != self._notion_gen or self.secrets.get(TOKENS["notion"]) != current:
                return False
            self._write_notion(data, access)
            self.accounts.set_expiry("notion", expires_at)
            return True

    def _rejected(self, gen: int, current: str | None) -> str | None:
        """Notion rejected the refresh of `current`: mark the account needs_reauth, unless
        the connection changed meanwhile (then the token now stored, if any)."""
        with self._notion_lock:
            now_token = self.secrets.get(TOKENS["notion"])
            if gen == self._notion_gen and now_token == current:
                self.accounts.set_needs_reauth("notion")
                return None
            return now_token if now_token and now_token != current else None

    async def can_refresh_notion(self) -> bool:
        return bool(await asyncio.to_thread(self.secrets.get, NOTION_REFRESH))

    async def refresh_notion(self, http: httpx.AsyncClient, stale: str | None) -> str:
        """A fresh Notion access token from the broker (once, even when several calls
        need one at the same time). NotionReauth (and the account marked needs_reauth)
        when Notion rejects it or there is nothing to refresh with; RunnerError when the
        broker or Notion can't be reached or is busy, needs_reauth unchanged."""
        async with self._refresh_lock:
            gen = self._notion_gen  # before reading the token: a change after it shows
            current = await asyncio.to_thread(self.secrets.get, TOKENS["notion"])
            if current and current != stale:  # someone else refreshed it meanwhile
                return current
            refresh = await asyncio.to_thread(self.secrets.get, NOTION_REFRESH)
            if not refresh or not self.settings.notion_broker_url:
                # Nothing to renew it with: as good as rejected, the user connects again.
                newer = await asyncio.to_thread(self._rejected, gen, current)
                if newer:
                    return newer
                raise NotionReauth
            try:
                resp = await http.post(f"{self._broker()}/notion/refresh",
                                       json={"refresh_token": refresh})  # fmt: skip
            except httpx.HTTPError as e:
                self.network.note_failure("notion", e)
                log.info("Notion's token couldn't be refreshed: the broker is out of reach")
                raise RunnerError(RENEW_LATER, code="notion") from None
            self.network.note_ok("notion")
            data = _json(resp)
            access = data.get("access_token")
            if resp.status_code in (400, 401):  # rejected (invalid_grant): connect again
                log.info("Notion rejected the refresh (the broker answered %s)",
                         resp.status_code)  # fmt: skip
                newer = await asyncio.to_thread(self._rejected, gen, current)
                if newer:  # pasted or signed in again meanwhile: use that
                    return newer
                raise NotionReauth
            if resp.status_code == 429 or resp.status_code >= 500:
                log.info("Notion's token couldn't be refreshed (the broker answered %s)",
                         resp.status_code)  # fmt: skip
                raise RunnerError(RENEW_LATER, code="notion")
            if resp.status_code != 200 or not isinstance(access, str) or not access:
                log.info("Notion's token couldn't be refreshed (the broker answered %s)",
                         resp.status_code)  # fmt: skip
                raise RunnerError(f"Notion's sign-in couldn't be renewed: the sign-in service "
                                  f"answered {resp.status_code}", transient=False,
                                  code="notion")  # fmt: skip
            if not await asyncio.to_thread(self._keep_refreshed, gen, current, data, access,
                                           _expiry(data)):  # fmt: skip
                log.info("Notion's connection changed during a refresh: its answer was dropped")
                now_token = await asyncio.to_thread(self.secrets.get, TOKENS["notion"])
                if now_token and now_token != current:  # pasted or signed in again: use it
                    return now_token
                raise NotionReauth  # disconnected
            log.info("Notion's token was refreshed")
            return access

    async def close(self) -> None:
        self._github_gen += 1  # a start still waiting for GitHub starts nothing
        await self._github_stop()


class NotionSession:
    """Notion calls with the stored token: refreshed first when it is about to expire,
    and once more when Notion answers 401, then the call is retried once."""

    def __init__(self, oauth: OAuth, http: httpx.AsyncClient) -> None:
        self.oauth = oauth
        self.http = http
        self.token: str | None = None
        self.refreshed = False

    async def open(self) -> bool:
        """False when Notion isn't connected. Raises NotionReauth when the token has
        (nearly) expired and can't be refreshed."""
        self.token = await asyncio.to_thread(self.oauth.secrets.get, TOKENS["notion"])
        if not self.token:
            return False
        expires_at = self.oauth.accounts.expires_at("notion")
        if expires_at is not None and expires_at - time.time() < REFRESH_MARGIN:
            self.refreshed = True
            self.token = await self.oauth.refresh_notion(self.http, self.token)
        return True

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}

    async def request(self, method: str, url: str, body: dict[str, Any]) -> httpx.Response:
        resp = await self._send(method, url, body)
        if (resp.status_code == 401 and not self.refreshed
                and await self.oauth.can_refresh_notion()):  # fmt: skip
            self.refreshed = True
            self.token = await self.oauth.refresh_notion(self.http, self.token)
            resp = await self._send(method, url, body)
        return resp

    async def _send(self, method: str, url: str, body: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self.http.request(method, url, headers=self.headers, json=body)
        except httpx.HTTPError as e:
            self.oauth.network.note_failure("notion", e)
            raise
        self.oauth.network.note_ok("notion")
        return resp


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:  # an HTML page from a proxy, an empty body
        return {}
    return data if isinstance(data, dict) else {}


def _str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _message(data: dict[str, Any]) -> str:
    for key in ("error_description", "message", "error"):
        if isinstance(data.get(key), str) and data[key]:
            return str(data[key])[:200]
    return "no reason given"


def _expiry(data: dict[str, Any]) -> float | None:
    expires_in = data.get("expires_in")
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        return time.time() + float(expires_in)
    return None


def callback_page(sentence: str) -> str:
    """The page the browser shows after Notion: no scripts, no external resources."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Newton</title><style>"
        "body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;font:16px/1.5 -apple-system,system-ui,sans-serif;"
        "background:#f7f7f5;color:#1f1f1f}"
        "@media (prefers-color-scheme:dark){body{background:#1c1c1e;color:#ececec}}"
        "main{max-width:30rem;padding:2rem 1rem;text-align:center}"
        "h1{font-size:1.25rem;margin:0 0 .5rem}"
        f"</style></head><body><main><h1>Newton</h1><p>{html.escape(sentence)}</p>"
        "</main></body></html>"
    )
