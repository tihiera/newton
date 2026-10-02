"""HTTP API consumed by the desktop UI. No business logic lives here."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..connectors.publish import PublishError
from ..context import AppContext
from ..contracts import (
    SAFE_TOKEN,
    AdvectionParams,
    Decision,
    ExperimentSpec,
    GoalCreate,
    GoalUpdate,
    HostCreate,
    HostKeyTrust,
    HostUpdate,
    ProfileUpdate,
    ServiceCreate,
)
from ..errors import NotFound
from ..orchestration import jobs as jobs_mod
from ..orchestration.state_machine import RESEARCH_ITEM, record_event
from ..research import schemes as schemes_mod
from ..research.experiment_design import BENCHMARKS
from ..research.papers import PaperError, proposal
from ..runners.ssh_config import list_config_hosts
from ..serving.router import PATHS as ROUTER_PATHS
from ..serving.router import RouterError
from ..storage.db import dumps, loads, new_id, now
from ..storage.files import resolve_inside

router = APIRouter()


def ctx_of(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


# -- health & events -----------------------------------------------------------


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    try:
        ctx.db.query_one("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "db": {"ok": db_ok, "schema_version": ctx.db.schema_version(), "path": str(ctx.db.path)},
        "scheduler": {"running": ctx.settings.start_scheduler, "ticks": ctx.scheduler.ticks},
        "uptime_seconds": time.time() - ctx.started_at,
    }


@router.get("/events")
async def events(
    request: Request,
    after: int = 0,
    limit: int = Query(200, le=1000),
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = ["id > ?"]
    params: list[Any] = [after]
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    rows = ctx_of(request).db.query(
        f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",  # noqa: S608
        (*params, limit),
    )
    return [{**r, "data": loads(r["data"])} for r in rows]


# -- hosts ---------------------------------------------------------------------


@router.get("/hosts")
async def list_hosts(request: Request) -> list[dict[str, Any]]:
    return ctx_of(request).hosts.list_hosts()


@router.post("/hosts", status_code=201)
async def create_host(request: Request, body: HostCreate) -> dict[str, Any]:
    return ctx_of(request).hosts.create(body)


@router.get("/hosts/{host_id}")
async def get_host(request: Request, host_id: str) -> dict[str, Any]:
    return ctx_of(request).hosts.get(host_id)


@router.delete("/hosts/{host_id}", status_code=204)
async def delete_host(request: Request, host_id: str, force: bool = False) -> None:
    await ctx_of(request).hosts.delete(host_id, force=force)


@router.post("/hosts/{host_id}/check")
async def check_host(request: Request, host_id: str) -> dict[str, Any]:
    return await ctx_of(request).hosts.check(host_id)


@router.get("/hosts/{host_id}/hostkeys")
async def scan_host_keys(request: Request, host_id: str) -> list[dict[str, str]]:
    return await ctx_of(request).hosts.scan_host_keys(host_id)


@router.post("/hosts/{host_id}/hostkeys/trust")
async def trust_host_keys(
    request: Request, host_id: str, body: HostKeyTrust
) -> list[dict[str, str]]:
    return await ctx_of(request).hosts.trust_host_keys(host_id, body.fingerprints)


@router.patch("/hosts/{host_id}")
async def update_host(request: Request, host_id: str, body: HostUpdate) -> dict[str, Any]:
    return await ctx_of(request).hosts.update(host_id, body)


@router.post("/hosts/{host_id}/gpu-support", status_code=202)
async def install_gpu_support(
    request: Request, response: Response, host_id: str, wait: bool = False
) -> dict[str, Any]:
    """Install CuPy matching the host's NVIDIA driver and verify it on the GPU.
    Returns at once (202; follow the host's `gpu_task`), or with `wait=true` the
    outcome (200, or an error with the host's own reason)."""
    host = await ctx_of(request).hosts.install_gpu_support(host_id, wait=wait)
    if wait:
        response.status_code = 200
    return host


@router.get("/services")
async def list_services(request: Request, host_id: str | None = None) -> list[dict[str, Any]]:
    return ctx_of(request).services.list(host_id)


@router.post("/services", status_code=201)
async def create_service(request: Request, body: ServiceCreate) -> dict[str, Any]:
    """Run a model server on a host. Needs approval when it downloads the model or
    runs model-supplied code; refused up front when the host lacks memory or disk."""
    return await ctx_of(request).services.create(body)


@router.get("/services/{service_id}")
async def get_service(request: Request, service_id: str) -> dict[str, Any]:
    return ctx_of(request).services.get(service_id)


@router.post("/services/{service_id}/stop")
async def stop_service(request: Request, service_id: str) -> dict[str, Any]:
    return await ctx_of(request).services.stop(service_id)


@router.post("/services/{service_id}/drain")
async def drain_service(
    request: Request, service_id: str, seconds: float = Query(60, ge=0, le=86400)
) -> dict[str, Any]:
    return await ctx_of(request).services.drain(service_id, seconds)


@router.get("/services/{service_id}/logs")
async def service_logs(
    request: Request, service_id: str, offset: int = Query(0, ge=0)
) -> dict[str, Any]:
    return await ctx_of(request).services.logs(service_id, offset)


@router.get("/services/{service_id}/credentials")
async def service_credentials(request: Request, service_id: str) -> dict[str, Any]:
    """Base URL and API key of a serving service, for a client on this Mac."""
    return ctx_of(request).services.credentials(service_id)


@router.post("/hosts/{host_id}/bootstrap")
async def bootstrap_host(request: Request, host_id: str) -> dict[str, Any]:
    return await ctx_of(request).hosts.bootstrap(host_id)


class SelftestRequest(BaseModel):
    sleep: float = Field(default=0.0, ge=0, le=3600)
    fail: bool = False


@router.post("/hosts/{host_id}/selftest", status_code=201)
async def selftest(request: Request, host_id: str, body: SelftestRequest) -> dict[str, Any]:
    """User-initiated connectivity test job (fixed module, no approval needed)."""
    ctx = ctx_of(request)
    ctx.hosts.get(host_id)
    job_id = jobs_mod.create_job(
        ctx.db,
        ctx.settings,
        host_id=host_id,
        role="selftest",
        label="selftest",
        manifest=jobs_mod.selftest_manifest(body.sleep, body.fail),
        bundle=_empty_bundle(),
    )
    ctx.scheduler.wake()
    return jobs_mod.job_view(jobs_mod.get_row(ctx.db, job_id))


def _empty_bundle() -> bytes:
    from ..runners.bundle import build_bundle

    return build_bundle(None, {})


@router.post("/hosts/{host_id}/connect")
async def connect_host(request: Request, host_id: str) -> dict[str, Any]:
    """Trusted key → install/start worker → check → selftest job, in one call.
    409 with `fingerprints` when the host key still needs the user's approval."""
    ctx = ctx_of(request)
    out = await ctx.hosts.connect(host_id)
    ctx.scheduler.wake()
    return out


@router.get("/ssh/hosts")
async def ssh_config_hosts(request: Request) -> list[dict[str, Any]]:
    """Hosts from ~/.ssh/config (and its Includes, e.g. NVIDIA Sync's), so the UI
    can offer "pick a host" instead of typing one."""
    ctx = ctx_of(request)
    registered = {h["ssh_target"]: h["id"] for h in ctx.hosts.list_hosts() if h.get("ssh_target")}
    hosts = list_config_hosts(ctx.settings.ssh_config_path)
    return [
        {
            "alias": h.alias,
            "source": h.source,
            "hostname": h.hostname,
            "user": h.user,
            "port": h.port,
            "proxy": h.proxy,
            "config_file": h.config_file,
            "host_id": registered.get(h.alias),
            # POST /hosts only accepts aliases ssh can't misread as options.
            "addable": bool(SAFE_TOKEN.match(h.alias)) and not h.alias.startswith("-"),
        }
        for h in hosts
    ]


# -- goals ---------------------------------------------------------------------


def _goal_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "keywords": loads(row["keywords"]), "categories": loads(row["categories"]),
            "auto_propose": bool(row["auto_propose"])}  # fmt: skip


@router.get("/goals")
async def list_goals(request: Request) -> list[dict[str, Any]]:
    rows = ctx_of(request).db.query("SELECT * FROM goals ORDER BY created_at DESC")
    return [_goal_view(r) for r in rows]


@router.post("/goals", status_code=201)
async def create_goal(request: Request, body: GoalCreate) -> dict[str, Any]:
    db = ctx_of(request).db
    goal_id = new_id("goal")
    t = now()
    with db.tx():
        db.insert(
            "goals",
            {
                "id": goal_id,
                "title": body.title,
                "description": body.description,
                "keywords": dumps(body.keywords),
                "categories": dumps(body.categories),
                "poll_hours": body.poll_hours,
                "auto_propose": int(body.auto_propose),
                "created_at": t,
                "updated_at": t,
            },
        )
        record_event(db, "goal", goal_id, "created", {"title": body.title})
    return await get_goal(request, goal_id)


@router.get("/goals/{goal_id}")
async def get_goal(request: Request, goal_id: str) -> dict[str, Any]:
    row = ctx_of(request).db.query_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
    if row is None:
        raise NotFound(f"unknown goal {goal_id}")
    return _goal_view(row)


@router.patch("/goals/{goal_id}")
async def update_goal(request: Request, goal_id: str, body: GoalUpdate) -> dict[str, Any]:
    await get_goal(request, goal_id)
    fields = body.model_dump(exclude_none=True)
    for listed in ("keywords", "categories"):
        if listed in fields:
            fields[listed] = dumps(fields[listed])
    if "auto_propose" in fields:
        fields["auto_propose"] = int(fields["auto_propose"])
    if fields:
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        db = ctx_of(request).db
        with db.tx():
            db.execute(
                f"UPDATE goals SET {assignments} WHERE id = :_id",  # noqa: S608
                {**fields, "_id": goal_id},
            )
            record_event(db, "goal", goal_id, "updated", {"fields": sorted(fields)})
    return await get_goal(request, goal_id)


# -- benchmarks & experiments --------------------------------------------------


@router.get("/benchmarks")
async def list_benchmarks() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": b["description"],
            "metrics": b["metrics"],
            "params_schema": AdvectionParams.model_json_schema(),
        }
        for name, b in BENCHMARKS.items()
    ]


@router.get("/experiments")
async def list_experiments(request: Request, state: str | None = None) -> list[dict[str, Any]]:
    return ctx_of(request).experiments.list(state)


@router.post("/experiments", status_code=201)
async def create_experiment(request: Request, body: ExperimentSpec) -> dict[str, Any]:
    ctx = ctx_of(request)
    await ctx.hosts.ensure_checked("local")
    if body.host_id != "auto":
        ctx.hosts.get(body.host_id)  # 404 for unknown (or deleted) hosts
        await ctx.hosts.ensure_checked(body.host_id)
    else:
        await ctx.hosts.refresh_stale_online()  # don't place on a host that went away
    return ctx.experiments.create(body)


@router.get("/experiments/{exp_id}")
async def get_experiment(request: Request, exp_id: str) -> dict[str, Any]:
    return ctx_of(request).experiments.get(exp_id)


@router.post("/experiments/{exp_id}/cancel")
async def cancel_experiment(request: Request, exp_id: str) -> dict[str, Any]:
    ctx = ctx_of(request)
    out = ctx.experiments.cancel(exp_id)
    ctx.scheduler.wake()
    return out


@router.get("/experiments/{exp_id}/report", response_class=PlainTextResponse)
async def experiment_report(request: Request, exp_id: str) -> str:
    row = ctx_of(request).experiments.get_row(exp_id)
    if not row["report_path"]:
        raise NotFound("report not ready")
    return Path(row["report_path"]).read_text()


@router.get("/experiments/{exp_id}/report/files/{path:path}")
async def experiment_report_file(request: Request, exp_id: str, path: str) -> FileResponse:
    ctx = ctx_of(request)
    ctx.experiments.get_row(exp_id)
    target = resolve_inside(ctx.settings.reports_dir / exp_id, path)
    if not target.is_file():
        raise NotFound("no such file")
    return FileResponse(target)


# -- jobs ----------------------------------------------------------------------


@router.get("/jobs")
async def list_jobs(
    request: Request, state: str | None = None, experiment_id: str | None = None
) -> list[dict[str, Any]]:
    return jobs_mod.list_jobs(ctx_of(request).db, state=state, experiment_id=experiment_id)


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    return jobs_mod.job_view(jobs_mod.get_row(ctx_of(request).db, job_id))


@router.get("/jobs/{job_id}/logs")
async def job_logs(
    request: Request,
    job_id: str,
    stream: str = "stdout",
    offset: int = 0,
    limit: int = 65536,
) -> dict[str, Any]:
    ctx = ctx_of(request)
    jobs_mod.get_row(ctx.db, job_id)
    return jobs_mod.read_log(ctx.settings, job_id, stream, offset, limit)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str, force: bool = False) -> dict[str, Any]:
    """Cancel asks the worker and waits for it to confirm. force=true is for a host
    that is gone for good: the job is closed now and its remote state is unknown."""
    ctx = ctx_of(request)
    if force:
        jobs_mod.get_row(ctx.db, job_id)  # 404 first
        confirmed = await ctx.hosts.cancel_remote(job_id)  # best effort, a few seconds
        reason = "by user; the worker confirmed" if confirmed else "by user; remote state unknown"
        return jobs_mod.force_cancel(ctx.db, job_id, reason)
    out = jobs_mod.request_cancel(ctx.db, job_id)
    ctx.scheduler.wake()
    return out


@router.get("/jobs/{job_id}/artifacts")
async def job_artifacts(request: Request, job_id: str) -> list[str]:
    row = jobs_mod.get_row(ctx_of(request).db, job_id)
    root = Path(row["artifacts_dir"])
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


@router.get("/jobs/{job_id}/artifacts/{path:path}")
async def job_artifact_file(request: Request, job_id: str, path: str) -> FileResponse:
    row = jobs_mod.get_row(ctx_of(request).db, job_id)
    target = resolve_inside(Path(row["artifacts_dir"]), path)
    if not target.is_file():
        raise NotFound("no such artifact")
    return FileResponse(target)


# -- approvals -----------------------------------------------------------------


@router.get("/approvals")
async def list_approvals(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    return ctx_of(request).approvals.list(status)


@router.post("/approvals/{approval_id}/approve")
async def approve(
    request: Request, approval_id: str, body: Decision | None = None
) -> dict[str, Any]:
    ctx = ctx_of(request)
    out = ctx.approvals.decide(approval_id, True, body.note if body else None)
    ctx.scheduler.wake()
    return out


@router.post("/approvals/{approval_id}/reject")
async def reject(
    request: Request, approval_id: str, body: Decision | None = None
) -> dict[str, Any]:
    return ctx_of(request).approvals.decide(approval_id, False, body.note if body else None)


# -- profile (no accounts: one person on one Mac) ----------------------------------


@router.get("/profile")
async def get_profile(request: Request) -> dict[str, Any]:
    return ctx_of(request).profile.get()


@router.patch("/profile")
async def update_profile(request: Request, body: ProfileUpdate) -> dict[str, Any]:
    ctx = ctx_of(request)
    profile = ctx.profile.update(**body.model_dump(exclude_unset=True))
    if body.mac_models is False:  # off means off: Mac models running now stop too
        for service in ctx.services.list("local"):
            if service["spec"]["engine"] == "mlx" and service["state"] in (
                "awaiting_approval",
                "approved",
                "starting",
                "ready",
                "draining",
            ):
                await ctx.services.stop(service["id"])
    return profile


# -- router (SV3): OpenAI-compatible /v1 for every model service --------------------


def _router_credentials(ctx: AppContext, key: str) -> dict[str, Any]:
    return {
        "base_url": f"http://127.0.0.1:{ctx.settings.port}/v1",
        "api_key": key,
        "note": "inference only (/v1/*); the admin token stays with the app",
    }


@router.get("/router/credentials")
async def router_credentials(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    return _router_credentials(ctx, ctx.profile.router_key())


@router.post("/router/credentials/rotate")
async def rotate_router_credentials(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    return _router_credentials(ctx, ctx.profile.rotate_router_key())


@router.get("/router/status")
async def router_status(request: Request) -> dict[str, Any]:
    r = ctx_of(request).router
    return {"leases": r.leases(), "in_flight": r.in_flight(), "waiting": r.waiting(),
            "models": r.models()}  # fmt: skip


@router.get("/router/requests")
async def router_requests(
    request: Request, service_id: str | None = None, limit: int = Query(100, ge=1, le=1000)
) -> list[dict[str, Any]]:
    return ctx_of(request).router.requests(service_id, limit)


@router.get("/v1/models")
async def v1_models(request: Request) -> dict[str, Any]:
    return {"object": "list", "data": ctx_of(request).router.models()}


@router.get("/v1/models/{model_id:path}")
async def v1_model(request: Request, model_id: str) -> Any:
    for model in ctx_of(request).router.models():
        if model["id"] == model_id:
            return model
    return RouterError(404, f"no model service runs {model_id[:100]!r}",
                       "invalid_request_error", "model_not_found").response()  # fmt: skip


@router.post("/v1/{path:path}")
async def v1_post(request: Request, path: str) -> Response:
    return await ctx_of(request).router.handle(request, f"/v1/{path}")


@router.api_route("/v1/{path:path}", methods=["GET", "PUT", "PATCH", "DELETE", "HEAD"])
async def v1_other(request: Request, path: str) -> Response:
    """Anything else under /v1 answers in OpenAI's error shape too."""
    full = f"/v1/{path}"
    if full in ROUTER_PATHS:
        return RouterError(405, f"{request.method} is not allowed on {full}",
                           "invalid_request_error", "method_not_allowed").response()  # fmt: skip
    return RouterError(404, f"{full[:100]} is not served by Newton's router",
                       "invalid_request_error", "unknown_url").response()  # fmt: skip


# -- schemes as data (E2) -------------------------------------------------------------


@router.get("/schemes")
async def list_schemes(request: Request) -> list[dict[str, Any]]:
    """The library: the hand-written schemes and more, as SchemeIR documents."""
    return schemes_mod.library(ctx_of(request).settings.benchmarks_dir)


@router.post("/schemes/check")
async def check_scheme(request: Request) -> dict[str, Any]:
    """Validate a SchemeIR document: its canonical form, digest and generated code hash."""
    doc = await request.json()
    return schemes_mod.check(ctx_of(request).settings.benchmarks_dir, doc)


# -- papers (B4) ------------------------------------------------------------------------


class IngestRequest(BaseModel):
    ref: str = Field(min_length=1, max_length=300, description="arXiv id or arxiv.org URL")
    goal_id: str | None = None
    model: str | None = Field(default=None, max_length=256, description="default: profile's")


class ProposeRequest(BaseModel):
    host_id: str = "auto"
    backend: str = "auto"
    baseline: Literal["upwind", "lax_wendroff", "muscl_minmod", "muscl_vanleer"] = "upwind"
    initial_condition: Literal["sine", "gaussian", "square"] = "sine"


@router.post("/research/ingest", status_code=202)
async def ingest_paper(request: Request, body: IngestRequest) -> dict[str, Any]:
    try:
        return ctx_of(request).papers.ingest(body.ref, body.goal_id, body.model)
    except PaperError as e:
        raise ValueError(str(e)) from None


@router.get("/research/items")
async def list_papers(request: Request, goal_id: str | None = None) -> list[dict[str, Any]]:
    return ctx_of(request).papers.list(goal_id)


@router.get("/research/items/{item_id}")
async def get_paper(request: Request, item_id: str) -> dict[str, Any]:
    try:
        return ctx_of(request).papers.get(item_id)
    except KeyError:
        raise NotFound(f"no research item {item_id}") from None


@router.post("/research/items/{item_id}/propose", status_code=201)
async def propose_experiment(request: Request, item_id: str,
                             body: ProposeRequest) -> dict[str, Any]:  # fmt: skip
    """The experiment the card suggests, created for approval (nothing runs before)."""
    ctx = ctx_of(request)
    try:
        item = ctx.papers.get(item_id)
    except KeyError:
        raise NotFound(f"no research item {item_id}") from None
    try:
        spec = ExperimentSpec.model_validate(
            proposal(item, body.host_id, body.backend, body.baseline, body.initial_condition)
        )
    except PaperError as e:
        raise ValueError(str(e)) from None
    experiment = ctx.experiments.create(spec)
    RESEARCH_ITEM.transition(ctx.db, item_id, "carded", "experiment_planned",
                             {"updated_at": now()})  # fmt: skip
    record_event(ctx.db, "research_item", item_id, "proposed", {"experiment_id": experiment["id"]})
    return experiment


# -- the research loop (B5) -------------------------------------------------------------


@router.post("/goals/{goal_id}/poll")
async def poll_goal(request: Request, goal_id: str) -> dict[str, Any]:
    """Look for new papers for this goal now (the loop does it every poll_hours)."""
    await get_goal(request, goal_id)
    try:
        return await ctx_of(request).loop.poll(goal_id)
    except PaperError as e:
        raise ValueError(str(e)) from None


@router.get("/findings")
async def list_findings(request: Request, goal_id: str | None = None) -> list[dict[str, Any]]:
    """Scientific memory: what each tested scheme's experiment showed, claim by claim."""
    ctx = ctx_of(request)
    ctx.loop.record_findings()
    return ctx.loop.findings(goal_id)


# -- publishing (B6): GitHub and Notion, always approved first ----------------------------


class Connect(BaseModel):
    token: str = Field(min_length=20, max_length=300)


class PublishRequest(BaseModel):
    target: Literal["github", "notion"]
    destination: dict[str, Any] = Field(default_factory=dict)


@router.get("/connectors")
async def connectors(request: Request) -> dict[str, bool]:
    return ctx_of(request).publisher.connections()


@router.put("/connectors/{target}")
async def connect(request: Request, target: Literal["github", "notion"],
                  body: Connect) -> dict[str, bool]:  # fmt: skip
    """Store the target's API token in the secret store (never in the database)."""
    try:
        return ctx_of(request).publisher.connect(target, body.token)
    except PublishError as e:
        raise ValueError(str(e)) from None


@router.post("/connectors/github/import-gh")
async def import_gh(request: Request) -> dict[str, bool]:
    """Use the GitHub CLI's token (asked for explicitly: `gh auth token`)."""
    try:
        return await asyncio.to_thread(ctx_of(request).publisher.import_gh_token)
    except PublishError as e:
        raise ValueError(str(e)) from None


@router.delete("/connectors/{target}")
async def disconnect(request: Request, target: str) -> dict[str, bool]:
    return ctx_of(request).publisher.disconnect(target)


@router.post("/experiments/{exp_id}/publish", status_code=201)
async def publish(request: Request, exp_id: str, body: PublishRequest) -> dict[str, Any]:
    """Ask to publish the report: nothing is sent until the approval is approved."""
    try:
        return ctx_of(request).publisher.request(exp_id, body.target, body.destination)
    except KeyError:
        raise NotFound(f"unknown experiment {exp_id}") from None
    except PublishError as e:
        raise ValueError(str(e)) from None


@router.get("/publications")
async def publications(request: Request, experiment_id: str | None = None) -> list[dict[str, Any]]:
    return ctx_of(request).publisher.list(experiment_id)
