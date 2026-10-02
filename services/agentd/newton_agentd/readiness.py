"""What this Mac can do right now, as sentences the UI shows as they are (B7).

Fast and side-effect free: only cached state (the database, the router's view of the
services, the router key cached at startup). No network call, no secret-store read
(that can block or prompt), no probe of a remote host.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from . import __version__, runtime_check
from .serving.router import RouterError

if TYPE_CHECKING:
    from .context import AppContext

ACTIONS = {
    "open_models": "Open Models",
    "open_compute": "Connect a GPU box",
    "open_settings": "Open Settings",
    "new_experiment": "Compare built-in schemes",
}
CHOOSE_READER = "Choose a reader model"  # the reader's own label for open_models
SECRET_STORES = {
    "keychain": "Tokens are kept in the macOS Keychain.",
    "memory": "Tokens are kept in memory only: they are gone when Newton stops.",
    "file": "Tokens are kept in a private file (a development setup).",
}


def item(
    key: str,
    state: str,
    title: str,
    detail: str,
    action: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    shown = {"kind": action, "label": label or ACTIONS[action]} if action else None
    return {"key": key, "state": state, "title": title, "detail": detail, "action": shown}


def sentence(text: str) -> str:
    """text as a sentence: capitalized (unless its first word is a name like arXiv), with
    a full stop."""
    text = text.strip().rstrip(".")
    first = text.split(" ", 1)[0]
    if first.isalpha() and first.islower():
        text = text[:1].upper() + text[1:]
    return f"{text}." if text else ""


def engine(ctx: AppContext) -> dict[str, Any]:
    runtime = runtime_check.check(ctx.settings)
    if runtime["problems"]:
        problems = " ".join(sentence(p) for p in runtime["problems"])
        detail = f"The engine is running, but parts of it are missing. {problems}"
        return item("engine", "warn", "Engine", detail)
    where = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    detail = f"The engine (Newton {__version__}) is running on {where}."
    return item("engine", "ok", "Engine", detail)


def cpu() -> dict[str, Any]:
    if not runtime_check.numpy_available():
        detail = f"{runtime_check.NUMPY_MISSING}, so experiments can't run on this Mac's CPU."
        return item("cpu", "missing", "CPU runs", detail)
    detail = "Experiments can run on this Mac's CPU."
    return item("cpu", "ok", "CPU runs", detail, "new_experiment")


def metal() -> dict[str, Any]:
    title = "Apple GPU (Metal)"
    reason = runtime_check.metal_problem()
    if reason:
        detail = f"{sentence(reason)} Experiments use the CPU or a GPU box instead."
        return item("metal", "missing", title, detail)
    return item("metal", "ok", title, "Experiments can run on this Mac's GPU.")


def reader(ctx: AppContext) -> dict[str, Any]:
    title = "Paper reader"
    model = ctx.profile.get()["default_model"]
    if not model:
        detail = "No reader model is chosen: papers can't be read until one is."
        return item("reader", "missing", title, detail, "open_models", CHOOSE_READER)
    try:
        _, _, serves = ctx.router.resolve(model)
    except RouterError as e:
        if e.status == 404:  # model_not_found: nothing runs it
            detail = f"No model service runs {model}: papers can't be read until one does."
            return item("reader", "missing", title, detail, "open_models", CHOOSE_READER)
        # Something runs it, but the router can't choose (several revisions): Models.
        detail = f"{sentence(e.message)} Papers wait until the reader names one."
        return item("reader", "warn", title, detail, "open_models")
    serving = {e.service_id for e in ctx.router.endpoints() if serves(e)}
    services = [s for m in ctx.router.models() for s in m["newton"]["services"]]
    if any(s["id"] in serving and s["routable"] for s in services):
        return item("reader", "ok", title, f"Papers are read by {model}.")
    detail = (f"{model} isn't answering yet (starting, or paused for a timed run): papers "
              "wait until it is.")  # fmt: skip
    return item("reader", "warn", title, detail, "open_models")


def gpu_host(ctx: AppContext) -> dict[str, Any]:
    title = "GPU box"
    remote = [h for h in ctx.hosts.list_hosts() if h["kind"] == "ssh"]
    online = [h for h in remote if h["status"] == "online"]
    ready = [h for h in online if h["capabilities"]["cuda"].get("ok")]
    if ready:
        names = ", ".join(h["name"] for h in ready)
        return item("gpu_host", "ok", title, f"{names}: online, with CUDA ready.")
    if online:
        host = online[0]
        why = sentence(str(host["capabilities"]["cuda"].get("reason") or "CUDA isn't ready"))
        return item("gpu_host", "ok", title, f"{host['name']} is online. {why}")
    if remote:
        host = remote[0]
        why = str(host.get("last_error") or "it hasn't answered yet").strip().rstrip(".")
        detail = f"{host['name']} isn't reachable ({why}): experiments run on this Mac."
        return item("gpu_host", "warn", title, detail, "open_compute")
    detail = "No GPU box is connected: experiments run on this Mac."
    return item("gpu_host", "warn", title, detail, "open_compute")


def keychain(ctx: AppContext) -> dict[str, Any]:
    title = "Keychain"
    if ctx.profile.cached_router_key() is None:  # read at startup: never a prompt here
        detail = ("The secret store didn't answer (is the Keychain locked?): connectors and "
                  "the model router's key wait until it does.")  # fmt: skip
        return item("keychain", "missing", title, detail, "open_settings")
    detail = SECRET_STORES.get(ctx.settings.secret_backend, "Tokens are in the secret store.")
    return item("keychain", "ok", title, detail)


def readiness(ctx: AppContext) -> dict[str, Any]:
    """GET /readiness: one item per part, in a fixed order."""
    return {"items": [engine(ctx), cpu(), metal(), reader(ctx), gpu_host(ctx), keychain(ctx)]}
