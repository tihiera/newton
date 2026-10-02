"""One-click Connect: GitHub's device flow and Notion's OAuth through the broker, the
accounts behind GET /connectors, and Notion's token refresh. GitHub, Notion and the
broker are mocked (httpx.MockTransport through the Publisher's http factory)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import AUTH, make_settings, wait_for
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.connectors import oauth, oauth_apps
from newton_agentd.errors import Conflict

GH_TOKEN = "gho_" + "g" * 36
NOTION_ACCESS = "ntn_" + "a" * 40
NOTION_REFRESH = "nrt_" + "r" * 40
NEW_ACCESS = "ntn_" + "n" * 40
NEW_REFRESH = "nrt_" + "s" * 40
PASTED = "ntn_" + "p" * 40
CODE = "notion-code-" + "c" * 24
BROKER = "https://broker.example"
CALLBACK = "http://127.0.0.1:8765/connectors/notion/callback"
PAGE = "0123456789abcdef0123456789abcdef"
SECRETS = [GH_TOKEN, NOTION_ACCESS, NOTION_REFRESH, NEW_ACCESS, NEW_REFRESH, PASTED, CODE]
STATES: list[str] = []  # every state value handed out, for the log check
INVALID = {"object": "error", "status": 401, "message": "API token is invalid."}
LAB = {"object": "page", "id": PAGE, "url": "https://notion.so/p",
       "properties": {"title": {"type": "title", "title": [{"plain_text": "Lab"}]}}}  # fmt: skip


class Gate:
    """Holds the requests to one URL until opened: the moment to race something in."""

    def __init__(self) -> None:
        self.reached = 0
        self.opened = asyncio.Event()


class Remote:
    """GitHub, Notion and the broker, as far as Newton uses them. Scripts say what the
    next answers are; every call is recorded. A gate holds the requests to a URL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.gates: dict[str, Gate] = {}
        self.device = 0
        self.device_answer: tuple[int, dict[str, Any]] | None = None  # None: a fresh code
        self.device_expires_in = 900
        self.polls: list[dict[str, Any]] = [{"access_token": GH_TOKEN, "token_type": "bearer"}]
        self.token_answer: tuple[int, dict[str, Any]] = (
            200,
            notion_tokens(NOTION_ACCESS, NOTION_REFRESH),
        )
        self.refresh_answer: tuple[int, dict[str, Any]] = (
            200,
            notion_tokens(NEW_ACCESS, NEW_REFRESH),
        )
        self.refresh_down = False  # the broker out of reach for /notion/refresh
        self.notion_valid: set[str] = {NOTION_ACCESS, NEW_ACCESS, PASTED}
        self.pages_401 = 0  # how many POST /pages answer 401 first

    def of(self, suffix: str) -> list[tuple[str, str, dict[str, Any], dict[str, str]]]:
        return [c for c in self.calls if c[1].endswith(suffix)]

    def gate(self, suffix: str) -> Gate:
        self.gates[suffix] = Gate()
        return self.gates[suffix]

    def factory(self) -> Any:
        async def gated(request: httpx.Request) -> httpx.Response:
            for suffix, gate in self.gates.items():
                if str(request.url).endswith(suffix):
                    gate.reached += 1
                    await gate.opened.wait()
            return handler(request)

        def handler(request: httpx.Request) -> httpx.Response:
            raw = request.content.decode() if request.content else ""
            if request.headers.get("content-type", "").startswith("application/x-www-form"):
                body: dict[str, Any] = {k: v[0] for k, v in parse_qs(raw).items()}
            else:
                body = json.loads(raw or "{}")
            url = str(request.url)
            self.calls.append((request.method, url, body, dict(request.headers)))
            if url == "https://github.com/login/device/code":
                if self.device_answer is not None:
                    return httpx.Response(self.device_answer[0], json=self.device_answer[1])
                self.device += 1
                return httpx.Response(200, json={
                    "device_code": f"dc-{self.device}", "user_code": f"CODE-{self.device:04d}",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": self.device_expires_in, "interval": 5})  # fmt: skip
            if url == "https://github.com/login/oauth/access_token":
                answer = self.polls.pop(0) if self.polls else {"error": "authorization_pending"}
                return httpx.Response(200, json=answer)
            if url == "https://api.github.com/user":
                return httpx.Response(200, json={
                    "login": "octocat", "avatar_url": "https://avatars.example/u/1"})  # fmt: skip
            if url == f"{BROKER}/notion/token":
                return httpx.Response(self.token_answer[0], json=self.token_answer[1])
            if url == f"{BROKER}/notion/refresh":
                if self.refresh_down:
                    raise httpx.ConnectError("no route to the broker")
                return httpx.Response(self.refresh_answer[0], json=self.refresh_answer[1])
            if url.startswith("https://api.notion.com/v1/"):
                token = request.headers.get("authorization", "").removeprefix("Bearer ")
                if token not in self.notion_valid:
                    return httpx.Response(401, json=INVALID)
                if url.endswith("/search"):
                    return httpx.Response(200, json={"object": "list", "results": [LAB]})
                if url.endswith("/pages"):
                    if self.pages_401:
                        self.pages_401 -= 1
                        return httpx.Response(401, json=INVALID)
                    return httpx.Response(200, json={"id": "page-1", "url": "https://notion.so/p1"})
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"message": "not mocked"})

        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(gated))


def notion_tokens(access: str, refresh: str, expires_in: int | None = 3600) -> dict[str, Any]:
    out: dict[str, Any] = {
        "access_token": access, "token_type": "bearer", "refresh_token": refresh,
        "bot_id": "bot-1", "workspace_name": "Fluids Lab", "workspace_icon": "🌊",
        "workspace_id": "ws-1", "owner": {"type": "user"},
    }  # fmt: skip
    if expires_in is not None:
        out["expires_in"] = expires_in
    return out


class Sleeper:
    """The device-flow poller's sleep: records each interval, returns at once, and can
    be held (a flow that is still waiting)."""

    def __init__(self) -> None:
        self.intervals: list[float] = []
        self.hold = False

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)
        while self.hold:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)


@pytest.fixture
def oauth_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, github_client_id="Iv1.0123456789abcdef",
                         notion_client_id="notion-client-id", notion_broker_url=BROKER + "/",
                         notion_redirect_uri="", port=8765)  # fmt: skip


@pytest.fixture
def app_client(oauth_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(oauth_settings), headers=AUTH) as c:
        yield c


@pytest.fixture
def remote(app_client: TestClient) -> Remote:
    r = Remote()
    app_client.app.state.ctx.publisher.http_factory = r.factory()  # type: ignore[attr-defined]
    return r


@pytest.fixture
def sleeper(app_client: TestClient) -> Sleeper:
    s = Sleeper()
    app_client.app.state.ctx.publisher.oauth.sleep = s  # type: ignore[attr-defined]
    return s


@pytest.fixture(autouse=True)
def nothing_secret_logged(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.DEBUG)
    STATES.clear()
    yield
    # The test's own client plays the browser: its request lines are not agentd's logs.
    logged = "\n".join(r.getMessage() for r in caplog.get_records("call")
                       if "http://testserver/" not in r.getMessage())  # fmt: skip
    for value in [*SECRETS, *STATES, "dc-1", "dc-2"]:
        assert value not in logged


def ctx(client: TestClient) -> Any:
    return client.app.state.ctx  # type: ignore[attr-defined]


def secret(client: TestClient, ref: str) -> str | None:
    value: str | None = ctx(client).secrets.get(ref)
    return value


def on_disk(settings: Settings, *values: str) -> list[str]:
    """Values found in any file under the data dir (the database, its WAL, reports)."""
    blobs = [p.read_bytes() for p in settings.data_dir.rglob("*") if p.is_file()]
    return [v for v in values if any(v.encode() in b for b in blobs)]


@contextmanager
def anonymous(client: TestClient) -> Iterator[None]:
    """Requests without the bearer token, as a browser tab sends them."""
    del client.headers["Authorization"]
    try:
        yield
    finally:
        client.headers.update(AUTH)


def device_done(client: TestClient) -> dict[str, Any]:
    status: dict[str, Any] = wait_for(lambda: client.get("/connectors/github/device").json(),
                                      lambda s: s["state"] != "pending", timeout=10)  # fmt: skip
    return status


def authorize(client: TestClient) -> str:
    r = client.post("/connectors/notion/authorize")
    assert r.status_code == 201
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    STATES.append(state)
    return state


def callback(client: TestClient, **params: str) -> httpx.Response:
    with anonymous(client):
        return client.get("/connectors/notion/callback", params=params)


# -- GitHub: device flow ------------------------------------------------------------------


def test_github_device_flow(app_client: TestClient, remote: Remote, sleeper: Sleeper,
                            oauth_settings: Settings) -> None:  # fmt: skip
    pending = {"error": "authorization_pending"}
    remote.polls = [pending, pending, {"access_token": GH_TOKEN, "token_type": "bearer"}]
    r = app_client.post("/connectors/github/device")
    assert r.status_code == 201
    start = r.json()
    assert start["user_code"] == "CODE-0001"
    assert start["verification_uri"] == "https://github.com/login/device"
    assert start["interval"] == 5
    assert abs(start["expires_at"] - (time.time() + 900)) < 5
    method, url, body, headers = remote.calls[0]
    assert (method, url) == ("POST", "https://github.com/login/device/code")
    assert body == {"client_id": "Iv1.0123456789abcdef", "scope": "gist public_repo"}
    assert headers["accept"] == "application/json"

    status = device_done(app_client)
    assert status["state"] == "connected" and status["error"] is None
    assert status["account"]["name"] == "octocat"
    assert status["account"]["icon"] == "https://avatars.example/u/1"
    assert status["account"]["method"] == "oauth"
    polls = remote.of("/login/oauth/access_token")
    assert len(polls) == 3
    grant = "urn:ietf:params:oauth:grant-type:device_code"
    assert polls[0][2] == {"client_id": "Iv1.0123456789abcdef", "device_code": "dc-1",
                           "grant_type": grant}  # fmt: skip
    assert sleeper.intervals == [5, 5, 5]
    user = remote.of("/user")[0]
    assert user[3]["authorization"] == f"Bearer {GH_TOKEN}"

    assert secret(app_client, "github-token") == GH_TOKEN
    connectors = app_client.get("/connectors").json()
    assert connectors["github"] is True
    assert connectors["accounts"]["github"]["name"] == "octocat"
    events = app_client.get("/events", params={"entity_type": "connector"}).json()
    assert [(e["entity_id"], e["kind"], e["data"]) for e in events] == [
        ("github", "connected", {"method": "oauth"})]  # fmt: skip
    assert GH_TOKEN not in json.dumps([start, status, connectors, events])
    assert on_disk(oauth_settings, GH_TOKEN, "dc-1") == []


def test_github_slow_down_waits_longer(app_client: TestClient, remote: Remote,
                                       sleeper: Sleeper) -> None:  # fmt: skip
    remote.polls = [{"error": "slow_down", "interval": 10}, {"error": "slow_down"},
                    {"error": "authorization_pending"}, {"access_token": GH_TOKEN}]  # fmt: skip
    app_client.post("/connectors/github/device")
    assert device_done(app_client)["state"] == "connected"
    assert sleeper.intervals == [5, 10, 15, 15]


@pytest.mark.parametrize(("answer", "state", "error"), [
    ({"error": "expired_token"}, "expired", "the code expired: start again"),
    ({"error": "access_denied"}, "denied", "you declined on GitHub"),
    ({"error": "unsupported_grant_type", "error_description": "The grant type isn't supported"},
     "failed", "GitHub refused the sign-in: The grant type isn't supported"),
])  # fmt: skip
def test_github_flow_ends(app_client: TestClient, remote: Remote, sleeper: Sleeper,
                          answer: dict[str, Any], state: str, error: str) -> None:  # fmt: skip
    remote.polls = [{"error": "authorization_pending"}, answer]
    app_client.post("/connectors/github/device")
    status = device_done(app_client)
    assert (status["state"], status["error"]) == (state, error)
    assert status["account"] is None and status["user_code"] is None
    assert secret(app_client, "github-token") is None
    assert app_client.get("/connectors").json()["accounts"]["github"] is None
    assert remote.of("/user") == []


def test_github_code_runs_out(app_client: TestClient, remote: Remote, sleeper: Sleeper) -> None:
    remote.device_expires_in = 0  # GitHub's code is already past its time
    app_client.post("/connectors/github/device")
    status = device_done(app_client)
    assert (status["state"], status["error"]) == ("expired", "the code expired: start again")
    assert remote.of("/login/oauth/access_token") == []


def test_github_not_configured(client: TestClient) -> None:
    ctx(client).settings.github_client_id = ""
    ctx(client).settings.notion_client_id = ""
    r = client.post("/connectors/github/device")
    assert r.status_code == 409
    assert r.json() == {"error": "GitHub sign-in isn't set up in this build: paste a token "
                                 "instead", "code": "not_configured"}  # fmt: skip
    r = client.post("/connectors/notion/authorize")
    assert r.status_code == 409
    assert r.json() == {"error": "Notion sign-in isn't set up in this build: paste a token "
                                 "instead", "code": "not_configured"}  # fmt: skip
    assert client.get("/connectors").json()["oauth"] == {"github": False, "notion": False}
    assert client.get("/connectors/github/device").json()["state"] == "none"


def test_notion_needs_the_broker_too(app_client: TestClient) -> None:
    ctx(app_client).settings.notion_broker_url = ""
    assert app_client.post("/connectors/notion/authorize").json()["code"] == "not_configured"
    assert app_client.get("/connectors").json()["oauth"] == {"github": True, "notion": False}


def test_github_refuses_the_device_code(app_client: TestClient, remote: Remote) -> None:
    remote.device_answer = (
        401,
        {
            "error": "incorrect_client_credentials",
            "error_description": "The client_id passed is incorrect.",
        },
    )
    r = app_client.post("/connectors/github/device")
    assert r.status_code == 502
    assert r.json()["error"] == "GitHub answered 401: The client_id passed is incorrect."
    remote.device_answer = (
        200,
        {"error": "device_flow_disabled", "error_description": "Device Flow must be enabled"},
    )
    r = app_client.post("/connectors/github/device")
    assert r.status_code == 502
    assert r.json()["error"] == "GitHub answered 200: Device Flow must be enabled"
    assert app_client.get("/connectors/github/device").json()["state"] == "none"


def test_a_second_sign_in_replaces_the_first(app_client: TestClient, remote: Remote,
                                             sleeper: Sleeper) -> None:  # fmt: skip
    sleeper.hold = True
    first = app_client.post("/connectors/github/device").json()
    second = app_client.post("/connectors/github/device").json()
    assert (first["user_code"], second["user_code"]) == ("CODE-0001", "CODE-0002")
    status = app_client.get("/connectors/github/device").json()
    assert status["state"] == "pending" and status["user_code"] == "CODE-0002"
    sleeper.hold = False
    assert device_done(app_client)["state"] == "connected"
    polled = [c[2]["device_code"] for c in remote.of("/login/oauth/access_token")]
    assert polled == ["dc-2"]  # the first flow was cancelled: never polled


def test_cancel_a_github_sign_in(app_client: TestClient, remote: Remote,
                                 sleeper: Sleeper) -> None:  # fmt: skip
    sleeper.hold = True
    app_client.post("/connectors/github/device")
    r = app_client.delete("/connectors/github/device")
    assert r.status_code == 200
    assert r.json() == {
        "state": "none",
        "error": None,
        "user_code": None,
        "verification_uri": None,
        "expires_at": None,
        "account": None,
    }
    sleeper.hold = False
    time.sleep(0.2)
    assert remote.of("/login/oauth/access_token") == []
    assert secret(app_client, "github-token") is None


# -- Notion: authorize and callback -------------------------------------------------------


def test_notion_authorize_url(app_client: TestClient) -> None:
    r = app_client.post("/connectors/notion/authorize")
    assert r.status_code == 201
    out = r.json()
    url = urlsplit(out["url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://api.notion.com/v1/oauth/authorize"
    query = parse_qs(url.query)
    state = query.pop("state")[0]
    STATES.append(state)
    assert query == {"client_id": ["notion-client-id"], "response_type": ["code"],
                     "owner": ["user"], "redirect_uri": [CALLBACK]}  # fmt: skip
    assert len(state) >= 43  # 32 random bytes
    assert abs(out["expires_at"] - (time.time() + 600)) < 5
    status = app_client.get("/connectors/notion/authorize").json()
    assert status["state"] == "pending" and status["expires_at"] == out["expires_at"]
    assert status["verification_uri"] == out["url"]  # to reopen it (the UI is authenticated)
    assert authorize(app_client) != state  # a fresh state each time


def test_notion_redirect_uri_follows_the_port(app_client: TestClient) -> None:
    ctx(app_client).settings.port = 9999
    url = app_client.post("/connectors/notion/authorize").json()["url"]
    redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
    # The address agentd binds, never "localhost" (which may be ::1, someone else's).
    assert redirect == "http://127.0.0.1:9999/connectors/notion/callback"
    ctx(app_client).settings.notion_redirect_uri = "http://127.0.0.1:9999/cb"
    url = app_client.post("/connectors/notion/authorize").json()["url"]
    assert parse_qs(urlsplit(url).query)["redirect_uri"] == ["http://127.0.0.1:9999/cb"]


def test_notion_callback(app_client: TestClient, remote: Remote, oauth_settings: Settings,
                         caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    state = authorize(app_client)
    r = callback(app_client, code=CODE, state=state)
    assert r.status_code == 200
    assert "Notion is connected. You can close this tab and go back to Newton." in r.text
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-type"].startswith("text/html")
    assert "<script" not in r.text and "src=" not in r.text and "href=" not in r.text
    for value in (NOTION_ACCESS, NOTION_REFRESH, CODE, state):
        assert value not in r.text

    [(method, url, body, _)] = remote.of("/notion/token")
    assert (method, url) == ("POST", f"{BROKER}/notion/token")
    assert body == {"code": CODE, "redirect_uri": CALLBACK}
    assert secret(app_client, "notion-token") == NOTION_ACCESS
    assert secret(app_client, "notion-refresh-token") == NOTION_REFRESH
    row = ctx(app_client).db.query_one("SELECT * FROM connector_accounts WHERE target = 'notion'")
    assert row["name"] == "Fluids Lab" and row["icon"] == "🌊" and row["method"] == "oauth"
    assert abs(row["expires_at"] - (time.time() + 3600)) < 5

    status = app_client.get("/connectors/notion/authorize").json()
    assert status["state"] == "connected"
    assert status["account"] == {"name": "Fluids Lab", "icon": "🌊", "method": "oauth",
                                 "connected_at": row["connected_at"],
                                 "needs_reauth": False}  # fmt: skip
    assert status["verification_uri"] is None
    connectors = app_client.get("/connectors").json()
    assert connectors["notion"] is True and connectors["accounts"]["notion"]["name"] == "Fluids Lab"
    events = app_client.get("/events", params={"entity_type": "connector"}).json()
    assert [(e["entity_id"], e["kind"], e["data"]) for e in events] == [
        ("notion", "connected", {"method": "oauth"})]  # fmt: skip
    everything = json.dumps([status, connectors, events])
    for value in (NOTION_ACCESS, NOTION_REFRESH, CODE, state):
        assert value not in everything
    assert on_disk(oauth_settings, NOTION_ACCESS, NOTION_REFRESH, CODE, state) == []
    assert "Notion connected" in caplog.text  # agentd's logs are captured (and checked)
    assert "https://broker.example/notion/token" in caplog.text

    again = callback(app_client, code=CODE, state=state)  # single use
    assert again.status_code == 400
    assert "This sign-in link has expired: start again from Newton" in again.text
    assert len(remote.of("/notion/token")) == 1


@pytest.mark.parametrize("params", [
    {"code": CODE, "state": "not-the-state"}, {"code": CODE}, {"code": CODE, "state": ""},
    {"error": "access_denied", "state": "forged"},
])  # fmt: skip
def test_notion_callback_unknown_state(app_client: TestClient, remote: Remote,
                                       params: dict[str, str]) -> None:  # fmt: skip
    authorize(app_client)
    r = callback(app_client, **params)
    assert r.status_code == 400
    assert "This sign-in link has expired: start again from Newton" in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert remote.of("/notion/token") == []
    assert app_client.get("/connectors/notion/authorize").json()["state"] == "pending"


def test_notion_callback_without_a_sign_in(app_client: TestClient, remote: Remote) -> None:
    r = callback(app_client, code=CODE, state="anything")
    assert r.status_code == 400 and remote.calls == []


def test_notion_link_expires(app_client: TestClient, remote: Remote) -> None:
    ctx(app_client).publisher.oauth.state_ttl = -1
    state = authorize(app_client)
    r = callback(app_client, code=CODE, state=state)
    assert r.status_code == 400
    assert "This sign-in link has expired: start again from Newton" in r.text
    status = app_client.get("/connectors/notion/authorize").json()
    assert status["state"] == "expired"
    assert status["error"] == "the sign-in link expired: start again"
    assert remote.calls == []
    authorize(app_client)  # expired without a callback: the status says so
    status = app_client.get("/connectors/notion/authorize").json()
    assert status["state"] == "expired"


def test_notion_access_denied(app_client: TestClient, remote: Remote) -> None:
    state = authorize(app_client)
    r = callback(app_client, error="access_denied", state=state)
    assert r.status_code == 200
    assert "You declined on Notion" in r.text
    status = app_client.get("/connectors/notion/authorize").json()
    assert (status["state"], status["error"]) == ("denied", "you declined on Notion")
    assert remote.calls == [] and secret(app_client, "notion-token") is None


def test_notion_broker_error(app_client: TestClient, remote: Remote) -> None:
    remote.token_answer = (400, {"error": "invalid_grant", "error_description": "Invalid code."})
    state = authorize(app_client)
    r = callback(app_client, code=CODE, state=state)
    assert r.status_code == 502
    assert "Notion couldn&#x27;t be connected: Notion answered 400: Invalid code." in r.text
    status = app_client.get("/connectors/notion/authorize").json()
    assert (status["state"], status["error"]) == ("failed", "Notion answered 400: Invalid code.")
    assert secret(app_client, "notion-token") is None
    assert app_client.get("/connectors").json()["accounts"]["notion"] is None


def test_notion_broker_unreachable(app_client: TestClient) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    publisher = ctx(app_client).publisher
    publisher.http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(down))
    r = callback(app_client, code=CODE, state=authorize(app_client))
    assert r.status_code == 502
    status = app_client.get("/connectors/notion/authorize").json()
    assert status["error"] == "Newton's Notion sign-in service couldn't be reached"


def test_callback_is_public_but_loopback_only(app_client: TestClient, remote: Remote) -> None:
    state = authorize(app_client)
    with anonymous(app_client):
        forged = app_client.get("/connectors/notion/callback", headers={"Host": "evil.example"},
                                params={"code": CODE, "state": state})  # fmt: skip
        assert forged.status_code == 403
        assert app_client.post("/connectors/notion/callback").status_code == 401  # GET only
        assert app_client.get("/connectors").status_code == 401
        assert app_client.post("/connectors/notion/authorize").status_code == 401
        assert app_client.get("/connectors/github/device").status_code == 401
        ok = app_client.get("/connectors/notion/callback", params={"code": CODE, "state": state},
                            headers={"Host": "localhost:8765"})  # fmt: skip
    assert ok.status_code == 200
    assert remote.of("/notion/token")


NONE = {"state": "none", "error": None, "user_code": None, "verification_uri": None,
        "expires_at": None, "account": None}  # fmt: skip


def test_cancel_a_notion_sign_in(app_client: TestClient, remote: Remote) -> None:
    assert app_client.delete("/connectors/notion/authorize").json() == NONE  # nothing pending
    state = authorize(app_client)
    r = app_client.delete("/connectors/notion/authorize")
    assert r.status_code == 200 and r.json() == NONE
    assert app_client.get("/connectors/notion/authorize").json() == NONE
    late = callback(app_client, code=CODE, state=state)  # the tab finishes after Cancel
    assert late.status_code == 400
    assert "This sign-in link has expired: start again from Newton" in late.text
    assert remote.calls == [] and secret(app_client, "notion-token") is None
    assert app_client.get("/connectors").json()["accounts"]["notion"] is None

    callback(app_client, code=CODE, state=authorize(app_client))  # connected: too late
    status = app_client.delete("/connectors/notion/authorize").json()
    assert status["state"] == "connected" and status["account"]["name"] == "Fluids Lab"
    assert secret(app_client, "notion-token") == NOTION_ACCESS


# -- Notion: refreshing the token ---------------------------------------------------------


def connect_notion(client: TestClient, remote: Remote, expires_in: int | None = 3600) -> None:
    remote.token_answer = (200, notion_tokens(NOTION_ACCESS, NOTION_REFRESH, expires_in))
    assert callback(client, code=CODE, state=authorize(client)).status_code == 200


def test_refresh_before_expiry(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote, expires_in=30)  # less than a minute left
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 200 and r.json()[0]["title"] == "Lab"
    [(_, _, body, _)] = remote.of("/notion/refresh")
    assert body == {"refresh_token": NOTION_REFRESH}
    [search] = remote.of("/search")
    assert search[3]["authorization"] == f"Bearer {NEW_ACCESS}"
    assert secret(app_client, "notion-token") == NEW_ACCESS
    assert secret(app_client, "notion-refresh-token") == NEW_REFRESH
    expires_at = ctx(app_client).publisher.oauth.accounts.expires_at("notion")
    assert abs(expires_at - (time.time() + 3600)) < 5
    app_client.get("/connectors/notion/pages")
    assert len(remote.of("/notion/refresh")) == 1  # fresh now: no second refresh


def test_no_refresh_while_the_token_is_good(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote, expires_in=None)  # no expiry known
    assert app_client.get("/connectors/notion/pages").status_code == 200
    assert remote.of("/notion/refresh") == []


def test_refresh_on_401_retries_once(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote)
    remote.notion_valid = {NEW_ACCESS}  # Notion no longer takes the first token
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 200
    assert [c[3]["authorization"] for c in remote.of("/search")] == [
        f"Bearer {NOTION_ACCESS}", f"Bearer {NEW_ACCESS}"]  # fmt: skip
    assert len(remote.of("/notion/refresh")) == 1

    remote.notion_valid = set()  # still 401 after a refresh: Notion's answer, no loop
    remote.refresh_answer = (200, notion_tokens(NOTION_ACCESS, NOTION_REFRESH))
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 502
    assert r.json()["error"] == "Notion answered 401: API token is invalid."
    assert len(remote.of("/notion/refresh")) == 2 and len(remote.of("/search")) == 4


def test_refresh_failure_asks_to_connect_again(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote, expires_in=10)
    remote.refresh_answer = (400, {"error": "invalid_grant"})
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 409
    assert r.json() == {"error": "Notion's sign-in expired: connect Notion again",
                        "code": "notion_reauth"}  # fmt: skip
    assert remote.of("/search") == []
    assert secret(app_client, "notion-token") == NOTION_ACCESS  # unchanged


def needs_reauth(client: TestClient) -> bool | None:
    account = client.get("/connectors").json()["accounts"]["notion"]
    return None if account is None else bool(account["needs_reauth"])


@pytest.mark.parametrize("status", [400, 401])
def test_a_rejected_refresh_needs_reauth(app_client: TestClient, remote: Remote,
                                         status: int) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=10)
    assert needs_reauth(app_client) is False
    remote.refresh_answer = (status, {"error": "invalid_grant"})
    assert app_client.get("/connectors/notion/pages").json()["code"] == "notion_reauth"
    assert needs_reauth(app_client) is True
    connectors = app_client.get("/connectors").json()
    assert connectors["notion"] is True  # still connected: the token is kept, but stale
    assert NOTION_REFRESH not in json.dumps(connectors)

    remote.refresh_answer = (200, notion_tokens(NEW_ACCESS, NEW_REFRESH))
    connect_notion(app_client, remote)  # connecting again clears it
    assert needs_reauth(app_client) is False


def test_paste_or_disconnect_clears_needs_reauth(app_client: TestClient,
                                                 remote: Remote) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=10)
    remote.refresh_answer = (400, {"error": "invalid_grant"})
    app_client.get("/connectors/notion/pages")
    assert needs_reauth(app_client) is True
    app_client.put("/connectors/notion", json={"token": PASTED})
    assert needs_reauth(app_client) is False
    connect_notion(app_client, remote, expires_in=10)
    app_client.get("/connectors/notion/pages")
    assert needs_reauth(app_client) is True
    app_client.delete("/connectors/notion")
    assert needs_reauth(app_client) is None
    connect_notion(app_client, remote)
    assert needs_reauth(app_client) is False


def test_github_never_needs_reauth(app_client: TestClient) -> None:
    app_client.put("/connectors/github", json={"token": GH_TOKEN})
    assert app_client.get("/connectors").json()["accounts"]["github"]["needs_reauth"] is False


@pytest.mark.parametrize("answer", [429, 500, 502, 503, "down"])
def test_a_transient_refresh_failure_is_not_reauth(app_client: TestClient, remote: Remote,
                                                   answer: int | str) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=10)
    if answer == "down":
        remote.refresh_down = True
    else:
        remote.refresh_answer = (int(answer), {"error": "temporarily_unavailable"})
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 502
    assert r.json() == {"error": "Notion couldn't be reached to renew the sign-in: try again "
                                 "in a moment", "code": "notion"}  # fmt: skip
    assert needs_reauth(app_client) is False
    assert secret(app_client, "notion-token") == NOTION_ACCESS
    assert secret(app_client, "notion-refresh-token") == NOTION_REFRESH
    remote.refresh_down = False  # back: the next call refreshes and goes through
    remote.refresh_answer = (200, notion_tokens(NEW_ACCESS, NEW_REFRESH))
    assert app_client.get("/connectors/notion/pages").status_code == 200
    assert secret(app_client, "notion-token") == NEW_ACCESS


def test_a_transient_refresh_failure_while_publishing(app_client: TestClient, remote: Remote,
                                                      tmp_path: Path) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=0)
    remote.refresh_answer = (503, {"error": "busy"})
    pub = publish_to_notion(app_client, tmp_path)
    assert pub["state"] == "failed"
    assert pub["error"] == ("Notion couldn't be reached to renew the sign-in: try again in a "
                            "moment")  # fmt: skip
    assert needs_reauth(app_client) is False and remote.of("/pages") == []


def test_another_refusal_is_neither(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote, expires_in=10)
    remote.refresh_answer = (403, {"error": "forbidden"})  # a broker set up wrong
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 502
    assert r.json()["error"] == ("Notion's sign-in couldn't be renewed: the sign-in service "
                                 "answered 403")  # fmt: skip
    assert needs_reauth(app_client) is False


def test_pasted_notion_token_is_not_refreshed(app_client: TestClient, remote: Remote) -> None:
    app_client.put("/connectors/notion", json={"token": PASTED})
    remote.notion_valid = set()
    r = app_client.get("/connectors/notion/pages")
    assert r.status_code == 502 and remote.of("/notion/refresh") == []


def reported(client: TestClient, tmp_path: Path) -> str:
    report = tmp_path / "report.md"
    report.write_text("# Result\n\nLax-Wendroff converges at second order.\n")
    t = time.time()
    ctx(client).db.insert("experiments", {
        "id": "exp-1", "host_id": "local", "title": "upwind vs lax-wendroff", "spec": "{}",
        "state": "reported", "evidence": "green", "report_path": str(report),
        "created_at": t, "updated_at": t})  # fmt: skip
    return "exp-1"


def publish_to_notion(client: TestClient, tmp_path: Path) -> dict[str, Any]:
    pub = client.post(
        f"/experiments/{reported(client, tmp_path)}/publish",
        json={"target": "notion", "destination": {"parent_page_id": PAGE}},
    ).json()
    approval = next(a for a in client.get("/approvals?status=pending").json()
                    if a["subject_id"] == pub["id"])  # fmt: skip
    client.post(f"/approvals/{approval['id']}/approve")
    done: dict[str, Any] = wait_for(
        lambda: next(p for p in client.get("/publications").json() if p["id"] == pub["id"]),
        lambda p: p["state"] in ("published", "failed"), timeout=10)  # fmt: skip
    return done


def test_publishing_refreshes_on_401(app_client: TestClient, remote: Remote,
                                     tmp_path: Path) -> None:  # fmt: skip
    connect_notion(app_client, remote)
    remote.pages_401 = 1
    pub = publish_to_notion(app_client, tmp_path)
    assert pub["state"] == "published" and pub["url"] == "https://notion.so/p1"
    pages = remote.of("/pages")
    assert [c[3]["authorization"] for c in pages] == [f"Bearer {NOTION_ACCESS}",
                                                      f"Bearer {NEW_ACCESS}"]  # fmt: skip
    assert pages[0][2] == pages[1][2]  # the same page, sent again


def test_publishing_with_an_expired_sign_in(app_client: TestClient, remote: Remote,
                                            tmp_path: Path) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=0)
    remote.refresh_answer = (401, {"error": "invalid_grant"})
    pub = publish_to_notion(app_client, tmp_path)
    assert pub["state"] == "failed"
    assert pub["error"] == "Notion's sign-in expired: connect Notion again"
    assert remote.of("/pages") == []
    assert app_client.get("/connectors").json()["accounts"]["notion"]["needs_reauth"] is True


def publish_issue(client: TestClient, exp: str, repo: str) -> dict[str, Any]:
    pub = client.post(f"/experiments/{exp}/publish", json={
        "target": "github", "destination": {"kind": "issue", "repo": repo}}).json()  # fmt: skip
    approval = next(a for a in client.get("/approvals?status=pending").json()
                    if a["subject_id"] == pub["id"])  # fmt: skip
    client.post(f"/approvals/{approval['id']}/approve")
    done: dict[str, Any] = wait_for(
        lambda: next(p for p in client.get("/publications").json() if p["id"] == pub["id"]),
        lambda p: p["state"] in ("published", "failed"), timeout=10)  # fmt: skip
    return done


def test_connect_reaches_public_repositories_only(
    app_client: TestClient, remote: Remote, sleeper: Sleeper, tmp_path: Path
) -> None:
    app_client.post("/connectors/github/device")
    assert device_done(app_client)["state"] == "connected"
    exp = reported(app_client, tmp_path)
    pub = publish_issue(app_client, exp, "lab/private-notes")  # GitHub answers 404
    assert pub["state"] == "failed"
    assert pub["error"] == ("GitHub couldn't find lab/private-notes: Connect reaches public "
                            "repositories only; paste a token with repo access for private "
                            "ones")  # fmt: skip
    [(_, url, _, headers)] = remote.of("/issues")
    assert url == "https://api.github.com/repos/lab/private-notes/issues"
    assert headers["authorization"] == f"Bearer {GH_TOKEN}"

    app_client.put("/connectors/github", json={"token": GH_TOKEN})  # pasted: GitHub's answer
    pub = publish_issue(app_client, exp, "lab/private-notes")
    assert pub["state"] == "failed" and pub["error"] == "GitHub answered 404: not mocked"


# -- accounts -----------------------------------------------------------------------------


def test_connectors_shape(app_client: TestClient, remote: Remote,
                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    assert app_client.get("/connectors").json() == {
        "github": False, "notion": False, "accounts": {"github": None, "notion": None},
        "oauth": {"github": True, "notion": True},
    }  # fmt: skip
    r = app_client.put("/connectors/github", json={"token": GH_TOKEN})
    account = r.json()["accounts"]["github"]
    assert account["method"] == "token" and account["name"] == "" and account["icon"] is None
    assert remote.calls == []  # pasting stays fast: nothing looked up

    def gh(argv: list[str], **_: Any) -> Any:
        assert argv == ["gh", "auth", "token"]
        return type("Done", (), {"stdout": GH_TOKEN + "\n"})()

    monkeypatch.setattr("newton_agentd.connectors.publish.subprocess.run", gh)
    r = app_client.post("/connectors/github/import-gh")
    assert r.json()["accounts"]["github"]["method"] == "gh"
    assert GH_TOKEN not in r.text


def test_disconnect_forgets_everything(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote)
    r = app_client.delete("/connectors/notion")
    assert r.status_code == 200
    assert r.json()["notion"] is False and r.json()["accounts"]["notion"] is None
    assert secret(app_client, "notion-token") is None
    assert secret(app_client, "notion-refresh-token") is None
    assert ctx(app_client).db.query("SELECT * FROM connector_accounts") == []
    app_client.put("/connectors/github", json={"token": GH_TOKEN})
    app_client.delete("/connectors/github")
    assert secret(app_client, "github-token") is None
    assert app_client.get("/connectors").json()["accounts"] == {"github": None, "notion": None}


def test_pasting_replaces_an_oauth_notion(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote, expires_in=10)
    app_client.put("/connectors/notion", json={"token": PASTED})
    assert secret(app_client, "notion-refresh-token") is None
    account = app_client.get("/connectors").json()["accounts"]["notion"]
    assert account["method"] == "token"
    assert ctx(app_client).publisher.oauth.accounts.expires_at("notion") is None
    assert app_client.get("/connectors/notion/pages").status_code == 200
    assert remote.of("/notion/refresh") == []


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (oauth.TOKENS["github"], oauth.NOTION_REFRESH) == (
        "github-token",
        "notion-refresh-token",
    )
    for name, value in (("NEWTON_GITHUB_CLIENT_ID", "gh-id"), ("NEWTON_NOTION_CLIENT_ID", "n-id"),
                        ("NEWTON_NOTION_BROKER_URL", "https://b"),
                        ("NEWTON_GITHUB_WEB", "http://127.0.0.1:1"),
                        ("NEWTON_GITHUB_API", "http://127.0.0.1:2"),
                        ("NEWTON_NOTION_API", "http://127.0.0.1:3/v1"),
                        ("NEWTON_PORT", "7000")):  # fmt: skip
        monkeypatch.setenv(name, value)
    s = Settings()
    assert (s.github_client_id, s.notion_client_id, s.notion_broker_url) == (
        "gh-id",
        "n-id",
        "https://b",
    )
    assert (s.github_web, s.github_api, s.notion_api) == (
        "http://127.0.0.1:1",
        "http://127.0.0.1:2",
        "http://127.0.0.1:3/v1",
    )
    assert s.notion_callback_url == "http://127.0.0.1:7000/connectors/notion/callback"
    monkeypatch.setenv("NEWTON_NOTION_REDIRECT_URI", "http://localhost:7000/x")
    assert Settings().notion_callback_url == "http://localhost:7000/x"
    assert oauth_apps.NOTION_REDIRECT_URI == ""  # empty: agentd's own callback
    monkeypatch.setattr("newton_agentd.config.NOTION_REDIRECT_URI", "http://127.0.0.1:7000/cb")
    assert Settings().notion_callback_url == "http://localhost:7000/x"  # the env still wins
    monkeypatch.delenv("NEWTON_NOTION_REDIRECT_URI")
    assert Settings().notion_callback_url == "http://127.0.0.1:7000/cb"  # the build's own
    for name in (
        "NEWTON_GITHUB_CLIENT_ID",
        "NEWTON_NOTION_CLIENT_ID",
        "NEWTON_NOTION_BROKER_URL",
        "NEWTON_GITHUB_WEB",
        "NEWTON_GITHUB_API",
        "NEWTON_NOTION_API",
    ):
        monkeypatch.delenv(name)
    s = Settings()
    assert (s.github_client_id, s.notion_client_id, s.notion_broker_url) == ("", "", "")
    assert (s.github_web, s.github_api, s.notion_api) == (
        "https://github.com",
        "https://api.github.com",
        "https://api.notion.com/v1",
    )


# -- races: overlapping sign-ins, a cancel at the wrong moment, a refresh in flight -------


def run(client: TestClient, scenario: Callable[[], Awaitable[Any]]) -> Any:
    """Run a scenario on agentd's own event loop (where its flows and pollers live)."""
    return client.portal.call(scenario)  # type: ignore[union-attr]


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def pollers() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if not t.done()
            and t.get_coro().__qualname__.endswith("_github_poll")]  # type: ignore[union-attr]  # fmt: skip


def test_overlapping_github_starts_leave_one_poller(app_client: TestClient, remote: Remote,
                                                    sleeper: Sleeper) -> None:  # fmt: skip
    o = ctx(app_client).publisher.oauth
    sleeper.hold = True

    async def scenario() -> None:
        gate = remote.gate("/login/device/code")
        first = asyncio.create_task(o.github_start())  # a double click on Connect
        second = asyncio.create_task(o.github_start())
        await until(lambda: gate.reached == 1)
        await asyncio.sleep(0.05)
        assert gate.reached == 1  # the second waits until the first has its poller
        gate.opened.set()
        a, b = await asyncio.gather(first, second)
        assert (a["user_code"], b["user_code"]) == ("CODE-0001", "CODE-0002")
        assert len(pollers()) == 1 and o.github_status()["user_code"] == "CODE-0002"
        sleeper.hold = False
        await until(lambda: o.github_status()["state"] != "pending")
        assert o.github_status()["state"] == "connected"
        await o.close()
        assert pollers() == []

    run(app_client, scenario)
    polled = {c[2]["device_code"] for c in remote.of("/login/oauth/access_token")}
    assert polled == {"dc-2"}  # the replaced flow never polled


def test_cancel_while_github_answers_starts_nothing(app_client: TestClient, remote: Remote,
                                                   sleeper: Sleeper) -> None:  # fmt: skip
    o = ctx(app_client).publisher.oauth

    async def scenario() -> None:
        gate = remote.gate("/login/device/code")
        start = asyncio.create_task(o.github_start())
        await until(lambda: gate.reached == 1)  # GitHub hasn't answered yet: Cancel
        assert (await o.github_cancel())["state"] == "none"
        gate.opened.set()
        with pytest.raises(Conflict) as e:
            await start
        assert (str(e.value), e.value.fields) == ("the GitHub sign-in was cancelled",
                                                  {"code": "cancelled"})  # fmt: skip
        assert pollers() == []
        await asyncio.sleep(0.05)
        del remote.gates["/login/device/code"]

    run(app_client, scenario)
    assert app_client.get("/connectors/github/device").json()["state"] == "none"
    assert remote.of("/login/oauth/access_token") == []
    assert secret(app_client, "github-token") is None
    assert app_client.post("/connectors/github/device").status_code == 201  # starts again


def test_cancel_after_the_token_finishes_the_connection(app_client: TestClient, remote: Remote,
                                                        sleeper: Sleeper) -> None:  # fmt: skip
    o = ctx(app_client).publisher.oauth

    async def scenario() -> dict[str, Any]:
        gate = remote.gate("/user")
        await o.github_start()
        await until(lambda: gate.reached == 1)  # the token is in; who is it? Cancel now
        cancel = asyncio.create_task(o.github_cancel())
        await asyncio.sleep(0.05)
        assert not cancel.done()  # it waits for the connection to be whole
        gate.opened.set()
        status: dict[str, Any] = await cancel
        return status

    status = run(app_client, scenario)
    assert status["state"] == "connected" and status["account"]["name"] == "octocat"
    assert secret(app_client, "github-token") == GH_TOKEN
    assert app_client.get("/connectors").json()["accounts"]["github"]["method"] == "oauth"
    events = app_client.get("/events", params={"entity_type": "connector"}).json()
    assert [(e["entity_id"], e["kind"]) for e in events] == [("github", "connected")]
    assert app_client.get("/connectors/github/device").json()["state"] == "connected"


def test_disconnect_during_a_refresh_stays_disconnected(app_client: TestClient,
                                                        remote: Remote) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=30)  # the next call refreshes first
    publisher = ctx(app_client).publisher

    async def scenario() -> None:
        gate = remote.gate("/notion/refresh")
        pages = asyncio.create_task(publisher.notion_pages())
        await until(lambda: gate.reached == 1)  # the broker is answering: Disconnect
        publisher.disconnect("notion")
        gate.opened.set()
        with pytest.raises(Conflict) as e:
            await pages
        assert e.value.fields == {"code": "notion_reauth"}

    run(app_client, scenario)
    assert secret(app_client, "notion-token") is None
    assert secret(app_client, "notion-refresh-token") is None
    connectors = app_client.get("/connectors").json()
    assert connectors["notion"] is False and connectors["accounts"]["notion"] is None
    assert remote.of("/search") == []


def test_paste_during_a_refresh_wins(app_client: TestClient, remote: Remote) -> None:
    connect_notion(app_client, remote, expires_in=30)
    publisher = ctx(app_client).publisher

    async def scenario() -> list[dict[str, Any]]:
        gate = remote.gate("/notion/refresh")
        pages = asyncio.create_task(publisher.notion_pages())
        await until(lambda: gate.reached == 1)
        publisher.connect("notion", PASTED)  # the user pastes a token meanwhile
        gate.opened.set()
        found: list[dict[str, Any]] = await pages
        return found

    assert run(app_client, scenario)[0]["title"] == "Lab"
    [search] = remote.of("/search")
    assert search[3]["authorization"] == f"Bearer {PASTED}"
    assert secret(app_client, "notion-token") == PASTED
    assert secret(app_client, "notion-refresh-token") is None
    assert app_client.get("/connectors").json()["accounts"]["notion"]["method"] == "token"
    assert publisher.oauth.accounts.expires_at("notion") is None


def test_cancel_while_the_broker_answers_drops_its_tokens(app_client: TestClient,
                                                          remote: Remote) -> None:  # fmt: skip
    o = ctx(app_client).publisher.oauth
    state = authorize(app_client)

    async def scenario() -> tuple[int, str]:
        gate = remote.gate("/notion/token")
        back = asyncio.create_task(o.notion_callback(CODE, state, None))
        await until(lambda: gate.reached == 1)  # the code is being traded: Cancel
        assert (await o.notion_cancel())["state"] == "none"
        gate.opened.set()
        answer: tuple[int, str] = await back
        return answer

    status, sentence = run(app_client, scenario)
    assert status == 400 and sentence == oauth.PAGE_CANCELLED
    assert secret(app_client, "notion-token") is None
    assert secret(app_client, "notion-refresh-token") is None
    assert app_client.get("/connectors").json()["accounts"]["notion"] is None
    assert app_client.get("/connectors/notion/authorize").json() == NONE
    assert app_client.get("/events", params={"entity_type": "connector"}).json() == []


def test_cancel_while_storing_finishes_the_connection(app_client: TestClient,
                                                      remote: Remote) -> None:  # fmt: skip
    o = ctx(app_client).publisher.oauth
    state = authorize(app_client)
    store, held = o._store_notion, asyncio.Event()
    reached: list[bool] = []

    async def slow_store(data: dict[str, Any], access: str) -> None:
        reached.append(True)
        await held.wait()
        await store(data, access)

    o._store_notion = slow_store

    async def scenario() -> dict[str, Any]:
        back = asyncio.create_task(o.notion_callback(CODE, state, None))
        await until(lambda: bool(reached))  # the tokens are in: Cancel now
        cancel = asyncio.create_task(o.notion_cancel())
        await asyncio.sleep(0.05)
        assert not cancel.done()  # it waits for the connection to be whole
        held.set()
        assert (await back)[0] == 200
        status: dict[str, Any] = await cancel
        return status

    status = run(app_client, scenario)
    assert status["state"] == "connected" and status["account"]["name"] == "Fluids Lab"
    assert secret(app_client, "notion-token") == NOTION_ACCESS


def test_a_rejection_after_a_paste_keeps_the_paste(app_client: TestClient,
                                                  remote: Remote) -> None:  # fmt: skip
    connect_notion(app_client, remote, expires_in=30)
    publisher = ctx(app_client).publisher
    remote.refresh_answer = (400, {"error": "invalid_grant"})

    async def scenario() -> list[dict[str, Any]]:
        gate = remote.gate("/notion/refresh")
        pages = asyncio.create_task(publisher.notion_pages())
        await until(lambda: gate.reached == 1)
        publisher.connect("notion", PASTED)  # pasted while the broker says no
        gate.opened.set()
        found: list[dict[str, Any]] = await pages
        return found

    assert run(app_client, scenario)[0]["title"] == "Lab"
    [search] = remote.of("/search")
    assert search[3]["authorization"] == f"Bearer {PASTED}"
    assert needs_reauth(app_client) is False  # the rejection was about the old sign-in
