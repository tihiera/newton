"""The router (SV3): one OpenAI-compatible `/v1/*` on agentd for every model service.

No roles and no accounts: a request names the model it wants and gets exactly that
model, served by one of the ready services that run it (never a substitute). Routing
only decides *which* service:

  - `model` is a served model name (`llama3.2:3b`; Ollama names also without
    `:latest`), optionally pinned to a revision (`llama3.2:3b@a80c4f17acd5`), a service
    id (`svc-...`), or `default` (the profile's default model, resolved on arrival);
  - one model served at two revisions is ambiguous unless the request pins one;
  - each service takes at most `parallel` requests at once (beyond that the engine only
    queues and time to first token grows to minutes, SV0). Requests wait in one queue,
    in arrival order, and each gets the least loaded free service for its model; a
    request whose services are all busy doesn't hold up one whose aren't. Waiting ends
    after the queue timeout (503), and at most `max_queue` requests wait per model (429);
  - a request that got no response at all (the connection failed or was closed before
    a status line: how a dead engine behind an SSH forward looks) is retried once on
    another service for the same model, and the service is re-probed at once;
  - a client that goes away, while waiting or mid-answer, frees its slot at once and
    the engine stops generating (its connection is closed).

Device leases: a timed benchmark (a GPU job, or one marked exclusive) takes its host for
itself. No new request goes to that host's services: requests for models served
elsewhere go there, the others are refused at once with a 503 that says why and when
to retry. Requests in flight there may finish; stragglers are aborted after the drain
timeout (and the job's timing gets a caveat). An idle resident model does not slow a
benchmark (SV0), so models stay loaded. Only traffic through the router is paused: a
client using a service's direct endpoint bypasses the lease.

Every response says what served it (`X-Newton-*` headers, ASCII ids only) and every
request is logged in `router_requests`: timings, status, token counts, error codes;
never prompt or completion text, nor an engine's error message (it can echo input).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ..config import Settings
from ..orchestration.state_machine import record_event
from ..profile import Profile
from ..secrets import SecretStore
from ..storage.db import Database, loads, new_id, now
from .manager import ServiceManager

log = logging.getLogger("newton_agentd.router")

PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings")
MAX_BODY = 32 * 1024 * 1024  # images for vision models travel inline as base64
MAX_MODEL_NAME = 256
SERVICE_ID = re.compile(r"svc-[0-9a-f]{6,32}")
ABORT_GRACE = 2.0  # seconds: engines notice closed connections; a stuck client is cut
ACTIVE = ("approved", "starting", "ready", "draining")
# A transport failure before any status line: the engine never got the request.
NO_RESPONSE = (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError,
               httpx.WriteError, httpx.ConnectTimeout)  # fmt: skip
# What an mlx-lm server may receive: the standard OpenAI request fields, nothing else.
MLX_FIELDS = frozenset({
    "model", "messages", "prompt", "input", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "top_k", "min_p", "stop", "stream", "stream_options", "seed",
    "presence_penalty", "frequency_penalty", "repetition_penalty", "logit_bias", "logprobs",
    "top_logprobs", "n", "response_format", "tools", "tool_choice", "user",
})  # fmt: skip
# ...of which only these say the endpoint itself is down (the others can be one
# broken connection, or an engine that crashed on this request).
DOWN = (httpx.ConnectError, httpx.ConnectTimeout)


class RouterError(Exception):
    """An error in the shape OpenAI clients understand."""

    def __init__(self, status: int, message: str, kind: str, code: str | None = None,
                 retry_after: int | None = None, retry: bool = True) -> None:  # fmt: skip
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.code = code
        self.retry_after = retry_after
        self.retry = retry

    def response(self, headers: dict[str, str] | None = None) -> Response:
        body = {"error": {"message": self.message, "type": self.kind, "param": None,
                          "code": self.code}}  # fmt: skip
        h = dict(headers or {})
        if self.retry_after is not None:
            h["Retry-After"] = str(min(60, self.retry_after))  # advice, kept short
        if not self.retry:
            h["x-should-retry"] = "false"
        return Response(json.dumps(body), status_code=self.status, headers=h,
                        media_type="application/json")  # fmt: skip


def _fail(waiter: Waiter, error: RouterError) -> None:
    waiter.future.set_exception(error)
    waiter.future.exception()  # its handler may be gone: never "exception never retrieved"


def paused(message: str) -> RouterError:
    # Not retried by the SDK: a benchmark can run for an hour; the caller decides.
    return RouterError(503, message, "server_error", "device_leased", retry_after=30,
                       retry=False)  # fmt: skip


def stopped(flight: Flight) -> RouterError:
    """Why a flight was cut short: a timed run took the host, or agentd is stopping."""
    if flight.reason == "shutdown":
        return RouterError(503, "agentd is shutting down", "server_error", "shutdown",
                           retry_after=5)  # fmt: skip
    return paused("aborted: a timed run needs this device")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


@dataclass
class Endpoint:
    service_id: str
    host_id: str
    engine: str
    model: str
    revision: str | None
    base_url: str
    parallel: int
    key_ref: str | None
    context_length: int | None = None


@dataclass(eq=False)  # identity: kept in sets
class Flight:
    """One request holding a slot on a service."""

    endpoint: Endpoint
    abort: asyncio.Event = field(default_factory=asyncio.Event)  # set: stop it now
    closed: asyncio.Event = field(default_factory=asyncio.Event)  # set: upstream closed
    released: bool = False
    reason: str = "lease"  # why abort was set: "lease" or "shutdown"


@dataclass
class Waiter:
    key: str
    serves: Callable[[Endpoint], bool]
    exclude: set[str]
    future: asyncio.Future[Flight]


@dataclass
class Lease:
    host_id: str
    job_id: str
    started: float = field(default_factory=time.monotonic)
    in_flight_at_start: int = 0
    aborted: int = 0
    loading_at_start: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    clear_since: float | None = None
    granted_at: float | None = None


def _plain(model: str) -> str:
    return model[: -len(":latest")] if model.endswith(":latest") else model


class Router:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        services: ServiceManager,
        profile: Profile,
        secrets: SecretStore,
    ) -> None:
        self.settings = settings
        self.db = db
        self.services = services
        self.profile = profile
        self.secrets = secrets
        self._flights: dict[str, set[Flight]] = {}  # service_id -> holding a slot
        self._queue: list[Waiter] = []  # arrival order
        self._leases: dict[str, Lease] = {}  # host_id -> lease
        self._keys: dict[str, asyncio.Future[str | None]] = {}  # service_id -> its engine key
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        services.on_change = self.dispatch  # ready / reachable / forward changes
        services.is_leased = lambda host_id: host_id in self._leases
        services.on_late_start = self._late_start

    # -- lifecycle -----------------------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                trust_env=False,  # never through a proxy: it would see prompts and keys
                timeout=httpx.Timeout(
                    connect=10, read=self.settings.router_read_timeout, write=60, pool=30
                ),  # fmt: skip
                limits=httpx.Limits(max_connections=512, max_keepalive_connections=64),
                headers={"Accept-Encoding": "identity"},  # relayed as is: never compressed
            )
        return self._http

    def start(self) -> None:
        self._task = asyncio.create_task(self._housekeeping(), name="newton-router")

    def begin_shutdown(self) -> None:
        """At the exit signal, before uvicorn waits for open requests: refuse new ones,
        empty the queue, cut what's in flight (each client gets a "shutdown" error)."""
        self._closing = True
        for waiter in self._queue:
            if not waiter.future.done():
                _fail(waiter, RouterError(503, "agentd is shutting down", "server_error",
                                          "shutdown", retry_after=5))  # fmt: skip
        self._queue.clear()
        for flights in self._flights.values():
            for flight in flights:
                if not flight.abort.is_set():
                    flight.reason = "shutdown"
                    flight.abort.set()

    async def close(self) -> None:
        self.begin_shutdown()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _housekeeping(self) -> None:
        """A safety net for changes nothing announced, and the log's retention."""
        last_prune = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                if self.profile.cached_router_key() is None:  # the keychain was locked
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.profile.router_key)
                self.dispatch()
                if time.monotonic() - last_prune > 600:
                    last_prune = time.monotonic()
                    self.prune_log()
            except Exception:
                log.exception("router housekeeping failed")

    # -- what can serve what ----------------------------------------------------------------
    def endpoints(self) -> list[Endpoint]:
        """Ready services with an endpoint on this Mac (not draining: those finish what
        they have and take nothing new), not known to be unreachable."""
        rows = self.db.query(
            "SELECT * FROM services WHERE state = 'ready' AND local_port IS NOT NULL "
            "ORDER BY created_at"
        )
        out = []
        for row in rows:
            if self.services.reachable(row["id"]) is False:
                continue
            spec = loads(row["spec"])
            out.append(Endpoint(
                service_id=row["id"], host_id=row["host_id"], engine=spec["engine"],
                model=spec["model"], revision=spec.get("revision"),
                base_url=f"http://127.0.0.1:{row['local_port']}/v1",
                parallel=int(spec.get("parallel") or 1), key_ref=row["api_key_ref"],
                context_length=self.services.context_limit(row["id"], spec.get("context_length")),
            ))  # fmt: skip
        return out

    def _known(self) -> list[dict[str, Any]]:
        """Services that exist and serve, or may soon."""
        rows = self.db.query(
            f"SELECT id, host_id, state, spec FROM services WHERE state IN "  # noqa: S608
            f"({', '.join('?' * len(ACTIVE))})",
            ACTIVE,
        )
        return [{"id": r["id"], "host_id": r["host_id"], "state": r["state"],
                 **loads(r["spec"])} for r in rows]  # fmt: skip

    def resolve(self, requested: str) -> tuple[str, str, Callable[[Endpoint], bool]]:
        """(the name resolved, a queue key, a test for endpoints serving exactly it)."""
        name = requested.strip()
        if name == "default":
            default = self.profile.get()["default_model"]
            if not default:
                raise RouterError(404, "no default model is set in the profile",
                                  "invalid_request_error", "model_not_found")  # fmt: skip
            name = default
        known = self._known()
        if SERVICE_ID.fullmatch(name):
            if not any(k["id"] == name for k in known):
                raise RouterError(404, f"no active model service {name}",
                                  "invalid_request_error", "model_not_found")  # fmt: skip
            return name, f"id:{name}", lambda e: e.service_id == name
        model, _, revision = name.partition("@")
        same = [k for k in known if _plain(k["model"]) == _plain(model)]
        if revision:
            same = [k for k in same if (k.get("revision") or "").startswith(revision)]
        if not same:
            available = sorted({k["model"] for k in known})[:50]
            raise RouterError(
                404,
                f"no model service runs {name!r}; available: {', '.join(available) or 'none'}",
                "invalid_request_error",
                "model_not_found",
            )
        revisions = sorted({str(k.get("revision")) for k in same})
        if len(revisions) > 1:
            raise RouterError(
                400,
                f"{model} is served at several revisions ({', '.join(revisions)}): "
                f"pin one as {model}@<revision>",
                "invalid_request_error",
                "ambiguous_model",
            )
        pinned, rev = same[0]["model"], same[0].get("revision")
        return name, f"model:{pinned}@{rev}", lambda e: e.model == pinned and e.revision == rev

    async def learn_context(self, requested: str) -> None:
        """Ask the Ollama services serving this model for the context their model was
        trained with (/api/show), once each: the service's own setting can be larger,
        and Ollama then drops the start of a longer prompt."""
        try:
            _, _, serves = self.resolve(requested)
        except RouterError:
            return
        for e in self.endpoints():
            if (
                e.engine != "ollama"
                or not serves(e)
                or self.services.knows_model_context(e.service_id)
            ):
                continue
            try:
                resp = await self.client().post(
                    e.base_url[: -len("/v1")] + "/api/show", json={"model": e.model}, timeout=15
                )
                info = resp.json().get("model_info") or {}
            except (httpx.HTTPError, ValueError, AttributeError):
                continue
            for key, value in info.items():
                if key.endswith(".context_length") and isinstance(value, int):
                    self.services.note_model_context(e.service_id, value)
                    break

    def slots(self, requested: str) -> int:
        """How many requests for this model can run at once now (its ready services'
        slots; 1 when none is known): how many parts of a paper are read together."""
        try:
            _, _, serves = self.resolve(requested)
        except RouterError:
            return 1
        return max(1, sum(e.parallel for e in self.endpoints() if serves(e)))

    def context_length(self, requested: str) -> int | None:
        """The context the model is served with: the smallest among the ready services
        serving it (a request may land on any), else among those that may soon serve
        it. None when unknown, or when no service runs it. Reads only."""
        try:
            _, _, serves = self.resolve(requested)
        except RouterError:
            return None
        lengths = [e.context_length for e in self.endpoints() if serves(e) and e.context_length]
        if not lengths:
            lengths = [n for k in self._known() if serves(_as_endpoint(k))
                       for n in [self.services.context_limit(k["id"], k.get("context_length"))]
                       if n]  # fmt: skip
        return min(lengths) if lengths else None

    def _refuse_if_paused(self, serves: Callable[[Endpoint], bool]) -> None:
        """Every service for this model is on a leased host: say so now (a benchmark can
        run for an hour) instead of holding the client in the queue."""
        mine = [k for k in self._known() if serves(_as_endpoint(k))]
        if mine and all(k["host_id"] in self._leases for k in mine):
            lease = self._leases[mine[0]["host_id"]]
            raise paused(
                f"paused: a timed run (job {lease.job_id}) has host {lease.host_id} to itself; "
                "requests resume when it ends"
            )

    def models(self) -> list[dict[str, Any]]:
        ready = {e.service_id for e in self.endpoints()}
        by_model: dict[str, dict[str, Any]] = {}
        for k in self._known():
            entry = by_model.setdefault(k["model"], {
                "id": k["model"], "object": "model", "created": 0, "owned_by": "newton",
                "newton": {"revisions": [], "services": []},
            })  # fmt: skip
            if k.get("revision") not in entry["newton"]["revisions"]:
                entry["newton"]["revisions"].append(k.get("revision"))
            entry["newton"]["services"].append({
                "id": k["id"], "host_id": k["host_id"], "state": k["state"],
                "engine": k["engine"], "revision": k.get("revision"),
                "routable": k["id"] in ready and k["host_id"] not in self._leases,
                "paused": k["host_id"] in self._leases,
                "in_flight": len(self._flights.get(k["id"], ())),
                "parallel": k.get("parallel"),
            })  # fmt: skip
        return list(by_model.values())

    def key_for(self, endpoint: Endpoint) -> asyncio.Future[str | None]:
        """The service's engine key, read once per service (one read shared by every
        request), off the event loop: a Keychain read can block or prompt."""
        fut = self._keys.get(endpoint.service_id)
        if fut is None or (fut.done() and (fut.cancelled() or fut.exception())):
            ref = endpoint.key_ref
            fut = asyncio.ensure_future(
                asyncio.to_thread(self.secrets.get, ref) if ref else _none()
            )
            self._keys[endpoint.service_id] = fut
        return fut

    # -- slots ------------------------------------------------------------------------------
    def in_flight(self, host_id: str | None = None) -> int:
        return sum(1 for fs in self._flights.values() for f in fs
                   if host_id is None or f.endpoint.host_id == host_id)  # fmt: skip

    def waiting(self) -> int:
        return len(self._queue)

    def dispatch(self) -> None:
        """Hand free slots to waiting requests, oldest first. Called on every change
        that can free or add capacity (a release, a lease ending, a service turning
        ready or reachable) and every second as a safety net."""
        try:
            self._dispatch()
        except Exception:  # never into a release or a request: retried within a second
            log.exception("router: dispatch failed")

    def _dispatch(self) -> None:
        self._queue = [w for w in self._queue if not w.future.done()]
        if not self._queue:
            return
        endpoints = self.endpoints()
        known = self._known()
        busy = {e.service_id: len(self._flights.get(e.service_id, ())) for e in endpoints}
        for waiter in list(self._queue):
            free = [
                e for e in endpoints
                if waiter.serves(e) and e.host_id not in self._leases
                and e.service_id not in waiter.exclude and busy[e.service_id] < e.parallel
            ]  # fmt: skip
            if free:
                e = min(free, key=lambda x: (busy[x.service_id] / x.parallel, busy[x.service_id]))
                busy[e.service_id] += 1
                flight = Flight(e)
                self._flights.setdefault(e.service_id, set()).add(flight)
                waiter.future.set_result(flight)
                self._queue.remove(waiter)
                continue
            mine = [k for k in known if waiter.serves(_as_endpoint(k))]
            untried = [k for k in mine if k["id"] not in waiter.exclude]
            if mine and not untried:  # its only services just failed to answer
                _fail(waiter, RouterError(502, "the model service could not be reached",
                                          "server_error", "upstream_unreachable"))  # fmt: skip
                self._queue.remove(waiter)
            elif not mine:
                _fail(waiter, RouterError(503, "the model is no longer served", "server_error",
                                          "model_gone", retry=False))  # fmt: skip
                self._queue.remove(waiter)
            elif all(k["host_id"] in self._leases for k in mine):
                lease = self._leases[mine[0]["host_id"]]
                _fail(waiter, paused(
                    f"paused: a timed run (job {lease.job_id}) has host {lease.host_id} to "
                    "itself; requests resume when it ends"
                ))  # fmt: skip
                self._queue.remove(waiter)

    async def acquire(self, key: str, serves: Callable[[Endpoint], bool], exclude: set[str],
                      gone: asyncio.Event) -> tuple[Flight, float]:  # fmt: skip
        """A slot on the least loaded service for this model, waiting in line if none is
        free. Gives up when the client goes away (`gone`)."""
        if sum(1 for w in self._queue if w.key == key) >= self.settings.router_max_queue:
            raise RouterError(429, "too many requests are waiting for this model",
                              "rate_limit_error", "queue_full", retry_after=5)  # fmt: skip
        if self._closing:
            raise RouterError(503, "agentd is shutting down", "server_error", "shutdown",
                              retry_after=5)  # fmt: skip
        t0 = time.monotonic()
        waiter = Waiter(key, serves, exclude, asyncio.get_running_loop().create_future())
        gone_wait: asyncio.Future[Any] = asyncio.ensure_future(gone.wait())
        watch: set[asyncio.Future[Any]] = {waiter.future, gone_wait}
        try:
            self._queue.append(waiter)
            self.dispatch()
            await asyncio.wait(watch, timeout=self.settings.router_queue_timeout,
                               return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        except BaseException:  # the handler itself was cancelled: leave the line
            if waiter.future.done() and not waiter.future.cancelled():
                if waiter.future.exception() is None:
                    self.release(waiter.future.result())
            else:
                waiter.future.cancel()
            raise
        finally:
            gone_wait.cancel()
        if waiter.future.done():
            flight = waiter.future.result()  # raises the waiter's RouterError, if any
            if gone.is_set():
                self.release(flight)
                raise _Gone
            return flight, time.monotonic() - t0
        waiter.future.cancel()
        self.dispatch()  # drops it from the queue
        if gone.is_set():
            raise _Gone
        raise RouterError(
            503, "no service for this model had a free slot within "
            f"{self.settings.router_queue_timeout:.0f} s", "server_error", "queue_timeout",
            retry=False,  # the SDK would put it back at the end of the queue, twice
        )  # fmt: skip

    def release(self, flight: Flight) -> None:
        """Synchronous and idempotent: callable first thing in any cleanup."""
        if flight.released:
            return
        flight.released = True
        flights = self._flights.get(flight.endpoint.service_id)
        if flights is not None:
            flights.discard(flight)
            if not flights:
                del self._flights[flight.endpoint.service_id]
        self.dispatch()

    # -- leases -----------------------------------------------------------------------------
    def lease(self, host_id: str, job_id: str) -> dict[str, Any]:
        """Take (or check) a host for a timed job; `granted` once nothing routed by
        Newton runs there and no model is loading there. In-flight requests may finish;
        after the drain timeout they are aborted, and the abort is recorded as a caveat."""
        lease = self._leases.get(host_id)
        if lease is None or lease.job_id != job_id:
            lease = Lease(host_id, job_id, in_flight_at_start=self.in_flight(host_id),
                          loading_at_start=self._loading(host_id))  # fmt: skip
            self._leases[host_id] = lease
            self.dispatch()  # waiters for models only served here are refused now
        if lease.granted_at is not None:
            return self.lease_view(lease)
        t = time.monotonic()
        flights = [f for fs in self._flights.values() for f in fs
                   if f.endpoint.host_id == host_id]  # fmt: skip
        loading = self._loading(host_id)
        overdue = t - lease.started >= self.settings.lease_drain_timeout
        if flights and overdue:
            for flight in flights:
                if not flight.abort.is_set():
                    flight.abort.set()
                    lease.aborted += 1
            note = f"{lease.aborted} model request(s) on this host were aborted for the run"
            lease.caveats = [c for c in lease.caveats if "were aborted" not in c]
            self._caveat(lease, note)
        if loading and overdue:
            self._caveat(lease, "a model was still loading on this host when the run started")
            loading = []
        if flights or loading:
            lease.clear_since = None
        elif lease.clear_since is None:
            lease.clear_since = t
        # After aborts, give the engines a moment to notice the closed connections.
        grace = ABORT_GRACE if lease.aborted else 0.0
        if lease.clear_since is not None and t - lease.clear_since >= grace:
            lease.granted_at = t
        return self.lease_view(lease)

    def _loading(self, host_id: str) -> list[str]:
        """Models loading on the host: services starting, and starts on their way."""
        rows = self.db.query(
            "SELECT id FROM services WHERE host_id = ? AND state = 'starting'", (host_id,)
        )
        return sorted({r["id"] for r in rows} | set(self.services.starting_on(host_id)))

    def _late_start(self, host_id: str, service_id: str) -> None:
        """A start that was already on its way when the host was leased."""
        lease = self._leases.get(host_id)
        if lease is not None:
            self._caveat(lease, f"model service {service_id} started loading during the run")

    def _caveat(self, lease: Lease, note: str) -> None:
        if note in lease.caveats:
            return
        lease.caveats.append(note)
        if lease.granted_at is not None:  # recorded already: record the change too
            record_event(self.db, "job", lease.job_id, "device_lease",
                         {**self.lease_view(lease), "update": True})  # fmt: skip

    def lease_view(self, lease: Lease) -> dict[str, Any]:
        flights = self.in_flight(lease.host_id)
        loading = self._loading(lease.host_id)
        waiting_on = None
        if lease.granted_at is None:
            waiting_on = "requests" if flights else "loading" if loading else "grace"
        return {
            "waiting_on": waiting_on,
            "loading": loading,
            "host_id": lease.host_id,
            "job_id": lease.job_id,
            "granted": lease.granted_at is not None,
            "in_flight": self.in_flight(lease.host_id),
            "in_flight_at_start": lease.in_flight_at_start,
            "loading_at_start": lease.loading_at_start,
            "aborted": lease.aborted,
            "caveats": lease.caveats,
            "waited_s": round((lease.granted_at or time.monotonic()) - lease.started, 3),
        }

    def lease_holder(self, host_id: str) -> str | None:
        lease = self._leases.get(host_id)
        return lease.job_id if lease else None

    def retain_leases(self, keep: dict[str, str]) -> None:
        """Drop every lease not in `keep` (host_id -> job_id): requests resume there."""
        dropped = [h for h, lease in self._leases.items() if keep.get(h) != lease.job_id]
        for host_id in dropped:
            del self._leases[host_id]
        if dropped:
            self.dispatch()
            self.services.wake()  # starts held back by the lease can go ahead

    def leases(self) -> list[dict[str, Any]]:
        return [self.lease_view(lease) for lease in self._leases.values()]

    # -- one request ------------------------------------------------------------------------
    async def handle(self, request: Request, path: str) -> Response:
        request_id = new_id("req")
        record: dict[str, Any] = {"id": request_id, "path": path[:100], "created_at": now(),
                                  "attempts": 0, "stream": 0}  # fmt: skip
        headers = {"X-Newton-Request-Id": request_id}
        gone = asyncio.Event()
        watcher: asyncio.Task[None] | None = None
        try:
            if self._closing:
                raise RouterError(503, "agentd is shutting down", "server_error", "shutdown",
                                  retry_after=5)  # fmt: skip
            if path not in PATHS:
                raise RouterError(404, f"{path[:100]} is not served by Newton's router",
                                  "invalid_request_error", "unknown_url")  # fmt: skip
            payload = _parse(await _read_body(request))
            model = payload.get("model")
            if not isinstance(model, str) or not model or len(model) > MAX_MODEL_NAME:
                raise RouterError(400, "the body must be a JSON object with a 'model' string "
                                  f"of at most {MAX_MODEL_NAME} characters",
                                  "invalid_request_error", "invalid_model")  # fmt: skip
            if payload.get("stream") not in (None, True, False):
                raise RouterError(400, "'stream' must be true or false",
                                  "invalid_request_error", "invalid_stream")  # fmt: skip
            record["model_requested"] = model
            stream = payload.get("stream") is True
            record["stream"] = int(stream)
            resolved, key, serves = self.resolve(model)
            if resolved != model:
                record["model_requested"] = f"{model} -> {resolved}"[:MAX_MODEL_NAME]
            self._refuse_if_paused(serves)
            watcher = asyncio.ensure_future(_watch_disconnect(request, gone))
            response = await self._forward(path, payload, stream, key, serves, record,
                                           headers, gone)  # fmt: skip
            if isinstance(response, _Relay):
                watcher.cancel()  # the streaming response listens for disconnects itself
                watcher = None
            return response
        except _Gone:
            record.update(status=499, error="client_disconnected")
            self._log(record)
            return Response(status_code=499)
        except RouterError as e:
            record.update(status=e.status, error=e.code or e.kind)
            self._log(record)
            return e.response(headers)
        except Exception:
            log.exception("router: request %s failed", request_id)
            record.update(status=500, error="internal_error")
            self._log(record)
            return RouterError(500, "internal error in Newton's router", "server_error",
                               "internal_error").response(headers)  # fmt: skip
        finally:
            if watcher is not None:
                watcher.cancel()

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A chat completion for Newton itself (paper extraction): the same model
        resolution, slots, device leases and provenance log as /v1, without HTTP.
        Returns {"body": the engine's JSON, "provenance": the X-Newton-* values}."""
        path = "/v1/chat/completions"
        request_id = new_id("req")
        record: dict[str, Any] = {"id": request_id, "path": f"{path} (newton)",
                                  "created_at": now(), "attempts": 0, "stream": 0}  # fmt: skip
        headers = {"X-Newton-Request-Id": request_id}
        try:
            model = str(payload["model"])[:MAX_MODEL_NAME]
            record["model_requested"] = model
            _, key, serves = self.resolve(model)
            self._refuse_if_paused(serves)
            response = await self._forward(path, {**payload, "stream": False}, False, key,
                                           serves, record, headers, asyncio.Event())  # fmt: skip
        except RouterError as e:
            if record.get("status") is None:
                record.update(status=e.status, error=e.code or e.kind)
                self._log(record)
            raise
        if response.status_code >= 400:
            raise RouterError(502, f"the model answered {response.status_code}", "server_error",
                              "upstream_error")  # fmt: skip
        body = json.loads(bytes(response.body))
        provenance = {k[len("X-Newton-") :].lower(): v for k, v in headers.items()}
        return {"body": body, "provenance": provenance}

    async def _forward(self, path: str, payload: dict[str, Any], stream: bool, key: str,
                       serves: Callable[[Endpoint], bool], record: dict[str, Any],
                       headers: dict[str, str], gone: asyncio.Event) -> Response:  # fmt: skip
        tried: set[str] = set()
        queued = 0.0
        while True:
            flight, waited = await self.acquire(key, serves, tried, gone)
            # From here until the answer (or the stream) is handed over, every way out
            # frees the slot: nothing may leak one.
            try:
                result = await self._attempt(flight, path, payload, stream, record, headers,
                                             gone, queued + waited)  # fmt: skip
            except BaseException:
                self.release(flight)
                raise
            if result is not None:
                return result
            queued += waited
            tried.add(flight.endpoint.service_id)
            if record["attempts"] >= 2:
                raise RouterError(502, "the model service could not be reached",
                                  "server_error", "upstream_unreachable")  # fmt: skip

    async def _attempt(self, flight: Flight, path: str, payload: dict[str, Any], stream: bool,
                       record: dict[str, Any], headers: dict[str, str], gone: asyncio.Event,
                       queued: float) -> Response | None:  # fmt: skip
        """One try on the service `flight` holds. None: it never got the request (try
        another). The slot is the caller's to free unless a relay took it over."""
        e = flight.endpoint
        record.update(service_id=e.service_id, host_id=e.host_id, engine=e.engine,
                      model=e.model, revision=e.revision, queued_ms=round(queued * 1000, 1),
                      attempts=record["attempts"] + 1)  # fmt: skip
        headers.update({"X-Newton-Service": e.service_id, "X-Newton-Host": e.host_id,
                        "X-Newton-Engine": e.engine, "X-Newton-Model": e.model,
                        "X-Newton-Revision": e.revision or ""})  # fmt: skip
        up_headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        try:
            api_key = await self._race(flight, gone, asyncio.shield(self.key_for(e)))
        except (_Gone, RouterError):
            raise
        except Exception:
            raise RouterError(503, "the secret store is unavailable (is the keychain locked?)",
                              "server_error", "secret_store") from None  # fmt: skip
        if api_key:
            up_headers["Authorization"] = f"Bearer {api_key}"
        # Serialised here: lone surrogates become escapes (ensure_ascii), never an error.
        # mlx-lm loads whatever path `model` (or `draft_model`, `adapters`) names: send
        # it only standard OpenAI fields, and only its own pinned snapshot.
        if e.engine == "mlx":
            forwarded = {k: v for k, v in payload.items() if k in MLX_FIELDS}
            forwarded["model"] = "default_model"
            asked = forwarded.get("max_tokens")
            limit = e.context_length or 8192
            forwarded["max_tokens"] = min(limit, asked if isinstance(asked, int) else 1024)
        else:
            forwarded = {**payload, "model": e.model}
            if e.engine != "ollama" and forwarded.get("reasoning_effort") == "none":
                del forwarded["reasoning_effort"]  # Ollama's "no thinking"; vLLM refuses it
        body = json.dumps(forwarded, ensure_ascii=True).encode()
        request = self.client().build_request(
            "POST", e.base_url + path[len("/v1") :], content=body, headers=up_headers
        )
        sent = time.monotonic()
        try:
            upstream = await self._race(flight, gone, self.client().send(request, stream=True))
        except NO_RESPONSE as exc:
            # The engine never got it (a forward being rebuilt, the host gone, one bad
            # connection): free the slot, have the service checked now, try another.
            self.release(flight)
            if isinstance(exc, DOWN):
                self.services.mark_unreachable(e.service_id)
            else:
                self.services.reprobe(e.service_id)
            return None
        except httpx.TimeoutException:
            raise RouterError(504, "the model service did not answer in time",
                              "server_error", "upstream_timeout") from None  # fmt: skip
        except httpx.HTTPError:
            raise RouterError(502, "the model service failed", "server_error",
                              "upstream_error") from None  # fmt: skip
        record["first_byte_ms"] = round((time.monotonic() - sent) * 1000, 1)
        sse = upstream.headers.get("content-type", "").startswith("text/event-stream")
        if stream and upstream.status_code == 200 and sse:
            return self._relay(flight, upstream, record, headers, sent)
        try:
            content = await self._race(flight, gone, upstream.aread())
        except httpx.TimeoutException:
            raise RouterError(504, "the model service did not answer in time",
                              "server_error", "upstream_timeout") from None  # fmt: skip
        except httpx.HTTPError:
            raise RouterError(502, "the model service failed mid-answer", "server_error",
                              "upstream_error") from None  # fmt: skip
        finally:
            self.release(flight)
            await _close(upstream, flight)
        record.update(status=upstream.status_code,
                      duration_ms=round((time.monotonic() - sent) * 1000, 1))  # fmt: skip
        return self._answer(upstream, content, record, headers, e)

    async def _race(self, flight: Flight, gone: asyncio.Event, work: Awaitable[Any]) -> Any:
        """Await `work` unless the client leaves (_Gone) or the flight is stopped (a
        lease, shutdown) first. Whatever way this ends, `work` is not left running."""
        task = asyncio.ensure_future(work)
        stops = [asyncio.ensure_future(gone.wait()), asyncio.ensure_future(flight.abort.wait())]
        try:
            await asyncio.wait({task, *stops}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            await _cancel(task)
            raise
        finally:
            for s in stops:
                s.cancel()
        if task.done():
            return task.result()
        await _cancel(task)  # closes the connection: the engine stops generating
        if gone.is_set():
            raise _Gone
        raise stopped(flight)

    def _answer(self, upstream: httpx.Response, content: bytes, record: dict[str, Any],
                headers: dict[str, str], e: Endpoint) -> Response:  # fmt: skip
        status = upstream.status_code
        if status in (401, 403):
            # The service's key, not the caller's: rotating the router key won't help.
            self.services.mark_unreachable(e.service_id)
            raise RouterError(502, "the model service refused Newton's key for it",
                              "server_error", "upstream_auth_failed")  # fmt: skip
        if status == 404:
            raise RouterError(502, "the model service doesn't have the model it should serve",
                              "server_error", "upstream_model_missing")  # fmt: skip
        if status >= 400:
            record["error"] = f"upstream_{status}"  # never the engine's text: it can echo input
            headers = {**headers, "X-Newton-Upstream-Status": str(status)}
        else:
            self._usage(record, content)
        self._log(record)
        return Response(content, status_code=status, headers=headers,
                        media_type=upstream.headers.get("content-type"))  # fmt: skip

    def _relay(self, flight: Flight, upstream: httpx.Response, record: dict[str, Any],
               headers: dict[str, str], sent: float) -> Response:  # fmt: skip
        state = {"done": False}

        def take(event: bytes) -> bytes:
            data = event.strip()
            if data in (b"data: [DONE]", b"data:[DONE]"):
                state["done"] = True
            elif b'"usage"' in event:
                self._usage(record, event.partition(b"data:")[2])
            return event + b"\n\n"

        async def events() -> AsyncIterator[bytes]:
            """Whole server-sent events only: a stream that breaks or is aborted ends
            with one well-formed error event (and no [DONE]), never a torn one."""
            buf = b""
            chunks = upstream.aiter_bytes()
            failure: tuple[str, str] | None = None
            nxt: asyncio.Future[bytes] | None = None
            try:
                while True:
                    nxt = asyncio.ensure_future(chunks.__anext__())
                    abort = asyncio.ensure_future(flight.abort.wait())
                    try:
                        await asyncio.wait({nxt, abort}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        abort.cancel()
                    if not nxt.done():
                        error = stopped(flight)
                        failure = (error.message, error.code or "aborted")
                        break
                    try:
                        chunk = nxt.result()
                    except StopAsyncIteration:
                        break
                    except httpx.HTTPError:
                        failure = ("the model service's stream broke", "upstream_disconnected")
                        break
                    # CRLF framing, even when a \r\n pair is split across two chunks.
                    buf = (buf + chunk).replace(b"\r\n", b"\n")
                    held = b""
                    if buf.endswith(b"\r"):
                        buf, held = buf[:-1], b"\r"
                    while b"\n\n" in buf:
                        event, buf = buf.split(b"\n\n", 1)
                        yield take(event)
                    buf += held
                if failure is None and buf.strip():
                    yield take(buf.rstrip(b"\r\n"))  # a last event without its blank line
                if failure is None and not state["done"]:
                    failure = ("the model service's stream ended early", "upstream_disconnected")
                if failure is not None:
                    record["error"] = failure[1]
                    yield _sse_error(*failure)
            finally:
                if nxt is not None:
                    await _cancel(nxt)  # never a read left running on a closing stream

        async def done() -> None:
            self.release(flight)
            await _close(upstream, flight)
            record.update(status=upstream.status_code,
                          duration_ms=round((time.monotonic() - sent) * 1000, 1))  # fmt: skip
            if not state["done"] and not record.get("error"):
                record["error"] = "client_disconnected"
            self._log(record)

        return _Relay(events(), done, flight, status_code=200, headers=headers,
                      media_type="text/event-stream")  # fmt: skip

    # -- provenance -------------------------------------------------------------------------
    @staticmethod
    def _usage(record: dict[str, Any], content: bytes) -> None:
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            usage = json.loads(content).get("usage") or {}
            if usage.get("prompt_tokens") is not None:
                record["prompt_tokens"] = int(usage["prompt_tokens"])
            if usage.get("completion_tokens") is not None:
                record["completion_tokens"] = int(usage["completion_tokens"])

    def _log(self, record: dict[str, Any]) -> None:
        record["finished_at"] = now()
        if record.get("duration_ms") is None:
            record["duration_ms"] = round((record["finished_at"] - record["created_at"]) * 1000, 1)
        cols = ", ".join(record)
        marks = ", ".join(f":{k}" for k in record)
        try:
            self.db.execute(
                f"INSERT OR REPLACE INTO router_requests ({cols}) VALUES ({marks})",  # noqa: S608
                record,
            )
        except Exception:
            log.exception("router: could not log request %s", record.get("id"))

    def prune_log(self) -> None:
        cutoff = now() - self.settings.router_log_days * 86400
        self.db.execute("DELETE FROM router_requests WHERE created_at < ?", (cutoff,))
        self.db.execute(
            "DELETE FROM router_requests WHERE id IN (SELECT id FROM router_requests "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (self.settings.router_log_rows,),
        )

    def requests(self, service_id: str | None, limit: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM router_requests WHERE (? IS NULL OR service_id = ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (service_id, service_id, limit),
        )
        return [dict(r) for r in rows]


def _as_endpoint(k: dict[str, Any]) -> Endpoint:
    return Endpoint(service_id=k["id"], host_id=k["host_id"], engine=k["engine"],
                    model=k["model"], revision=k.get("revision"), base_url="",
                    parallel=int(k.get("parallel") or 1), key_ref=None)  # fmt: skip


def _sse_error(message: str, code: str) -> bytes:
    body = {"error": {"message": message, "type": "server_error", "param": None, "code": code}}
    return f"data: {json.dumps(body)}\n\n".encode()


def _parse(body: bytes) -> dict[str, Any]:
    try:
        # NaN / Infinity are not JSON (and the engines would choke on them).
        payload = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise RouterError(400, "the body must be JSON", "invalid_request_error",
                          "invalid_json") from None  # fmt: skip
    if not isinstance(payload, dict):
        raise RouterError(400, "the body must be a JSON object", "invalid_request_error",
                          "invalid_json")  # fmt: skip
    return payload


async def _none() -> None:
    return None


async def _cancel(task: asyncio.Future[Any]) -> None:
    """Cancel and wait; if it finished with an open upstream response, close that."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.shield(task)
    if task.done() and not task.cancelled() and task.exception() is None:
        result = task.result()
        if isinstance(result, httpx.Response):
            with contextlib.suppress(BaseException):
                await result.aclose()


async def _read_body(request: Request) -> bytes:
    size = int(request.headers.get("content-length") or 0)
    if size > MAX_BODY:
        raise RouterError(413, "request body too large", "invalid_request_error", "too_large")
    parts, total = [], 0
    async for part in request.stream():
        total += len(part)
        if total > MAX_BODY:
            raise RouterError(413, "request body too large", "invalid_request_error",
                              "too_large")  # fmt: skip
        parts.append(part)
    return b"".join(parts)


async def _watch_disconnect(request: Request, gone: asyncio.Event) -> None:
    """uvicorn doesn't cancel a handler when its client leaves: watch for it."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            gone.set()
            return


async def _close(upstream: httpx.Response, flight: Flight) -> None:
    try:
        await asyncio.shield(upstream.aclose())
    except BaseException:  # noqa: S110 - closing is best effort; the slot is already free
        pass
    finally:
        flight.closed.set()


class _Gone(Exception):
    """The client went away."""


class _Relay(StreamingResponse):
    """A streaming response whose cleanup always runs: after the last event, after an
    error, and when the client disconnects mid-stream (the slot is freed first, with
    nothing awaited before it). A stop (lease, shutdown) is enforced from here too: a
    client that stopped reading leaves the generator parked in a send it never
    reaches the end of, so after a short grace the whole response is cancelled."""

    def __init__(self, content: AsyncIterator[bytes], done: Callable[[], Awaitable[None]],
                 flight: Flight, **kwargs: Any) -> None:  # fmt: skip
        super().__init__(content, **kwargs)
        self._done = done
        self._flight = flight

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        run = asyncio.ensure_future(super().__call__(scope, receive, send))
        stop = asyncio.ensure_future(self._flight.abort.wait())
        try:
            await asyncio.wait({run, stop}, return_when=asyncio.FIRST_COMPLETED)
            if not run.done():  # stopped: let it write its error event, then cut it
                await asyncio.wait({run}, timeout=ABORT_GRACE)
                await _cancel(run)
            elif run.exception() is not None:
                raise run.exception()  # type: ignore[misc]
        except BaseException:
            await _cancel(run)
            raise
        finally:
            stop.cancel()
            await asyncio.shield(self._done())
