"""B7: the runtime of a clone: its resources, the local host's capabilities, the
repository commit without developer tools, /health, and exiting with the shell."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, make_settings
from fastapi.testclient import TestClient
from newton_agentd import runtime_check
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.orchestration.capabilities import PlacementError, capabilities, place
from newton_agentd.runners import bundle

REPO = Path(__file__).resolve().parents[3]
MAC = {"arch": "arm64", "apple_gpu": {"name": "Apple M3 Max"}, "packages": {"mlx": "0.32.3"}}


def resources(root: Path, complete: bool = True) -> Path:
    """A resources folder without .git (benchmarks and the worker), e.g. a downloaded
    archive or NEWTON_RESOURCES_DIR pointing elsewhere."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "benchmarks").symlink_to(REPO / "benchmarks")
    (root / "services").mkdir()
    if complete:
        (root / "services" / "worker").symlink_to(REPO / "services" / "worker")
    return root


class Calls:
    """Stands in for subprocess.run in runners.bundle: records every argv."""

    def __init__(self, xcode_select: int = 0, developer_dir: str = "") -> None:
        self.argvs: list[list[str]] = []
        self.xcode_select = xcode_select
        self.developer_dir = developer_dir

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.argvs.append(list(argv))
        if argv[0] == "xcode-select":
            out = self.developer_dir + "\n" if self.xcode_select == 0 else ""
            return subprocess.CompletedProcess(argv, self.xcode_select, out, "")
        if argv[:4] == ["git", "-C", argv[2], "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def git(self) -> list[list[str]]:
        return [a for a in self.argvs if a[0] == "git"]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    fake = Calls()
    monkeypatch.setattr(bundle.subprocess, "run", fake)
    monkeypatch.setattr(bundle.sys, "platform", "darwin")
    return fake


# -- the repository commit -------------------------------------------------------------


def test_git_never_runs_without_a_checkout(tmp_path: Path, calls: Calls) -> None:
    assert bundle.repository_commit(tmp_path) is None
    assert calls.argvs == []  # not even xcode-select


def test_git_never_runs_without_developer_tools(tmp_path: Path, calls: Calls) -> None:
    (tmp_path / ".git").mkdir()
    calls.xcode_select = 2  # "unable to get active developer directory"
    assert bundle.repository_commit(tmp_path) is None
    calls.xcode_select, calls.developer_dir = 0, str(tmp_path / "gone")  # removed since
    assert bundle.repository_commit(tmp_path) is None
    assert calls.git == [] and calls.argvs == [["xcode-select", "-p"]] * 2


def test_git_runs_in_a_checkout_with_developer_tools(tmp_path: Path, calls: Calls) -> None:
    (tmp_path / ".git").mkdir()
    calls.developer_dir = str(tmp_path)
    assert bundle.repository_commit(tmp_path) == "abc123"
    assert calls.git == [
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--", "benchmarks"],
    ]


def test_creating_an_experiment_from_a_folder_without_git_never_runs_git(
    tmp_path: Path, calls: Calls
) -> None:
    settings = make_settings(tmp_path, resources_dir=resources(tmp_path / "Resources"))
    small = {"resolutions": [32, 64], "repeats": 1}
    spec = {
        "title": "t",
        "host_id": "local",
        "backend": "cpu",
        "variants": [
            {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind", **small}},
            {"role": "candidate", "label": "lw", "params": {"scheme": "lax_wendroff", **small}},
        ],
    }
    with TestClient(create_app(settings), headers=AUTH) as c:
        r = c.post("/experiments", json=spec)
        assert r.status_code == 201, r.text
    assert calls.argvs == []


# -- resources, /health ---------------------------------------------------------------


def test_resources_dir_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEWTON_RESOURCES_DIR", raising=False)
    assert Settings().resources_dir == REPO
    monkeypatch.setenv("NEWTON_RESOURCES_DIR", str(tmp_path))
    assert Settings().resources_dir == tmp_path


def test_runtime_check_names_each_missing_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEWTON_PACKAGED", "1")  # what a packaged app set: ignored now
    ok = runtime_check.check(make_settings(tmp_path, resources_dir=resources(tmp_path / "a")))
    assert ok == {"packaged": False, "resources_ok": True, "problems": []}

    partial = resources(tmp_path / "b", complete=False)
    out = runtime_check.check(make_settings(tmp_path, resources_dir=partial))
    assert out["resources_ok"] is False and len(out["problems"]) == 2
    assert out["problems"][0].startswith("services/worker/newton_worker/__init__.py (the worker)")
    assert "services/worker/bootstrap.sh" in out["problems"][1]

    gone = runtime_check.check(make_settings(tmp_path, resources_dir=tmp_path / "nope"))
    assert gone["resources_ok"] is False
    # The remedy is the clone's: there is no installer to reinstall from.
    for problem in [*out["problems"], *gone["problems"]]:
        assert "reinstall" not in problem
        assert "restore it from git" in problem and "NEWTON_RESOURCES_DIR" in problem
        assert problem.endswith("then run scripts/setup.sh.")


def test_health_reports_runtime_and_network(tmp_path: Path) -> None:
    partial = resources(tmp_path / "Resources", complete=False)
    with TestClient(create_app(make_settings(tmp_path, resources_dir=partial))) as c:
        body = c.get("/health").json()
        assert body["status"] == "ok"  # a runtime problem is reported, not fatal
        assert body["runtime"]["resources_ok"] is False and body["runtime"]["problems"]
        assert body["network"]["state"] in ("online", "offline", "unknown")
        assert set(body["network"]) >= {"state", "since", "detail"}
        ctx = c.app.state.ctx  # type: ignore[attr-defined]
        if not hasattr(ctx, "network"):  # until the offline unit wires ctx.network
            assert body["network"] == {"state": "unknown", "since": None, "detail": None}


# -- the local host's capabilities -----------------------------------------------------


def test_local_cpu_needs_numpy_in_agentds_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: False)
    local = capabilities(MAC, local=True)
    assert local["cpu"] == {
        "ok": False,
        "device": "arm64",
        "reason": "numpy is missing from Newton's runtime",
    }
    assert capabilities(None, local=True)["cpu"]["ok"] is False
    assert capabilities(MAC)["cpu"]["ok"] is True  # remote hosts: their own probe


def test_local_metal_reasons_are_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: True)
    monkeypatch.setattr(runtime_check.sys, "platform", "darwin")
    monkeypatch.setattr(runtime_check.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(runtime_check.platform, "mac_ver", lambda: ("13.6.1", ("", "", ""), ""))
    metal = capabilities(MAC, local=True)["metal"]
    assert metal["ok"] is False
    assert metal["reason"] == "MLX needs macOS 14 or later; this Mac runs macOS 13.6.1"

    monkeypatch.setattr(runtime_check.platform, "mac_ver", lambda: ("15.4", ("", "", ""), ""))
    monkeypatch.setattr(runtime_check, "_mlx_present", lambda: False)
    metal = capabilities(MAC, local=True)["metal"]
    assert metal == {
        "ok": False,
        "device": "Apple M3 Max",
        "reason": "Apple M3 Max found, but MLX is missing from Newton's runtime",
    }
    unchecked = capabilities(None, local=True)["metal"]
    assert unchecked["reason"] == runtime_check.MLX_MISSING

    monkeypatch.setattr(runtime_check, "_mlx_present", lambda: True)
    assert capabilities(MAC, local=True)["metal"] == {"ok": True, "device": "Apple M3 Max"}
    monkeypatch.setattr(runtime_check.platform, "machine", lambda: "x86_64")
    assert capabilities(None, local=True)["metal"]["reason"] == "no Apple GPU on this host"


def test_hosts_api_reports_the_local_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: False)
    local = client.get("/hosts/local").json()
    assert local["capabilities"]["cpu"]["reason"] == "numpy is missing from Newton's runtime"
    r = client.post(
        "/experiments",
        json={
            "title": "t",
            "host_id": "local",
            "backend": "cpu",
            "variants": [
                {"role": "baseline", "label": "a", "params": {"scheme": "upwind"}},
                {"role": "candidate", "label": "b", "params": {"scheme": "lax_wendroff"}},
            ],
        },
    )
    assert r.status_code in (400, 422) and "numpy is missing" in r.text, r.text


def test_local_metal_needs_numpy_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metal jobs import numpy (the solver and its backends): MLX alone isn't enough."""
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: False)
    monkeypatch.setattr(runtime_check, "metal_problem", lambda: None)
    assert capabilities(MAC, local=True)["metal"] == {
        "ok": False,
        "device": "Apple M3 Max",
        "reason": runtime_check.NUMPY_MISSING,
    }
    monkeypatch.setattr(runtime_check, "metal_problem", lambda: runtime_check.MLX_MISSING)
    metal = capabilities(MAC, local=True)["metal"]  # the more specific reason stays
    assert metal["reason"] == "Apple M3 Max found, but MLX is missing from Newton's runtime"
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: True)
    monkeypatch.setattr(runtime_check, "metal_problem", lambda: None)
    assert capabilities(MAC, local=True)["metal"]["ok"] is True


@pytest.mark.parametrize("backend", ["auto", "cpu", "metal"])
def test_auto_placement_names_the_missing_numpy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """No GPU host online and no numpy here: the refusal says numpy is the blocker."""
    monkeypatch.setattr(runtime_check, "numpy_available", lambda: False)
    monkeypatch.setattr(runtime_check, "metal_problem", lambda: None)
    small: dict[str, Any] = {"resolutions": [32, 64], "repeats": 1}
    if backend == "metal":
        small["precision"] = "float32"  # else refused before placement (no float64)
    r = client.post(
        "/experiments",
        json={
            "title": "t",
            "host_id": "auto",
            "backend": backend,
            "variants": [
                {"role": "baseline", "label": "a", "params": {"scheme": "upwind", **small}},
                {"role": "candidate", "label": "b", "params": {"scheme": "lax_wendroff", **small}},
            ],
        },
    )
    assert r.status_code in (400, 422), r.text
    detail = r.text
    assert runtime_check.NUMPY_MISSING in detail, detail
    assert not detail.rstrip().endswith(":"), detail


def test_placement_reasons_are_never_empty() -> None:
    """An explicit cpu request on "auto" that no host can take still says why."""
    host = {
        "id": "local",
        "name": "This Mac",
        "kind": "local",
        "status": "online",
        "capabilities": capabilities(None),
    }
    with pytest.raises(PlacementError) as err:
        place([host], "auto", "cpu", uses_float64=True, needs_gpu=True)
    assert str(err.value) == (
        "no connected host can run cpu: This Mac: the kernel implementation needs a GPU"
    )


# -- the agentd process ------------------------------------------------------------------


def test_readiness_never_claims_a_packaged_runtime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEWTON_PACKAGED", "1")
    engine = next(i for i in client.get("/readiness").json()["items"] if i["key"] == "engine")
    assert "Newton.app" not in engine["detail"] and "Python 3." in engine["detail"]
    assert client.get("/health").json()["runtime"]["packaged"] is False


def agentd(tmp_path: Path, *extra: str, port: str = "0") -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "NEWTON_SECRET_BACKEND": "memory",
        "NEWTON_API_TOKEN": "t",
        "NEWTON_KNOWN_HOSTS": str(tmp_path / "known_hosts"),
        "NEWTON_RESOURCES_DIR": str(REPO),
    }
    argv = [sys.executable, "-m", "newton_agentd.main", "serve", "--port", port]
    return subprocess.Popen(
        [*argv, "--data-dir", str(tmp_path / "data"), *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_agentd_exits_cleanly_when_the_shell_goes_away(tmp_path: Path) -> None:
    proc = agentd(tmp_path, "--exit-on-stdin-eof")
    try:
        assert proc.stdout is not None and proc.stdin is not None
        ready = json.loads(proc.stdout.readline())
        assert ready["event"] == "ready" and ready["url"].startswith("http://127.0.0.1:")
        proc.stdin.close()  # the desktop shell exited
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.stderr is not None
    log = proc.stderr.read()
    assert "stdin closed: shutting down" in log and "RUNTIME PROBLEM" not in log


def test_without_the_flag_stdin_eof_changes_nothing(tmp_path: Path) -> None:
    proc = agentd(tmp_path)
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert json.loads(proc.stdout.readline())["event"] == "ready"
        proc.stdin.close()
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.5)
    finally:
        proc.terminate()
        proc.wait(timeout=30)


@pytest.mark.parametrize("how", ["port in use", "interrupt"])
def test_agentd_with_the_flag_exits_without_aborting(tmp_path: Path, how: str) -> None:
    """Exits other than stdin EOF while the shell still holds stdin: a blocked read of
    stdin must not abort finalization (SIGABRT, "Fatal Python error")."""
    busy = socket.socket()
    try:
        if how == "port in use":
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = str(busy.getsockname()[1])
            proc = agentd(tmp_path, "--exit-on-stdin-eof", port=port)
            status = proc.wait(timeout=30)
            assert status > 0, status  # uvicorn's startup failure, not a signal
        else:
            proc = agentd(tmp_path, "--exit-on-stdin-eof")
            assert proc.stdout is not None
            assert json.loads(proc.stdout.readline())["event"] == "ready"
            proc.send_signal(signal.SIGINT)
            status = proc.wait(timeout=30)
            assert status in (0, 1, -signal.SIGINT, 128 + signal.SIGINT), status
    finally:
        busy.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.stderr is not None
    log = proc.stderr.read()
    assert status != -signal.SIGABRT and "Fatal Python error" not in log, log
    if how == "port in use":
        assert "address already in use" in log.lower(), log
