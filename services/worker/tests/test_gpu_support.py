"""GPU support (G4): `bootstrap.sh --gpu-only` installs CuPy for the host's NVIDIA
driver through the normal installer chain, smoke-tests it in a child process, and
records the outcome in gpu.json, which is what makes the cuda backend usable.

The GPU is fake (a pure-Python `cupy` wheel and an `nvidia-smi` script, see
pyenv_shims); the installs, the shell logic and the recording are real. Whatever
the GPU does, the worker keeps serving: GPU trouble is recorded, never fatal.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from newton_worker import gpu, run_job
from newton_worker import jobs as worker_jobs
from pyenv_shims import (  # noqa: F401 - fixtures
    FAKE_CUPY,
    FAKE_DRIVER,
    FAKE_NUMPY,
    OLD_CUPY,
    base_python,
    empty_wheelhouse,
    make_fake_nvidia_smi,
    pip_install_into,
    tool_path,
    wheelhouse,
)
from test_bootstrap import INSTALLER_CASES, SHELL, FakeHost, fake_wrapper


class GpuHost(FakeHost):
    def __init__(self, root: Path, wheelhouse: Path, tool_path: str) -> None:
        super().__init__(root, wheelhouse, tool_path)
        self.gpu_bin = root / "gpu-bin"
        self.gpu_bin.mkdir()
        self.env["PATH"] = f"{self.gpu_bin}:{tool_path}"

    def driver(self, version: str = FAKE_DRIVER, fail: str | None = None) -> None:
        make_fake_nvidia_smi(self.gpu_bin, version, fail)

    def fake_gpu(self, mode: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "fake-cupy").write_text(mode)

    def gpu(
        self, python: Path, mode: str = "auto", *flags: str, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """What agentd runs once the worker serves: the installed bootstrap.sh."""
        return subprocess.run(
            [SHELL, str(self.fp / "src" / "bootstrap.sh"), "--gpu-only", mode,
             "--python", str(python), *flags],
            env={**self.env, **env}, cwd=self.home, capture_output=True, text=True, timeout=300,
        )  # fmt: skip

    def gpu_ok(self, python: Path, mode: str = "auto", *flags: str, **env: str) -> dict[str, Any]:
        proc = self.gpu(python, mode, *flags, **env)
        assert proc.returncode == 0, proc.stderr
        printed = json.loads(proc.stdout.strip().splitlines()[-1])
        assert printed == self.gpu_state()  # the last line is gpu.json, for agentd
        return printed or {}

    def gpu_state(self) -> dict[str, Any] | None:
        path = self.fp / "gpu.json"
        return json.loads(path.read_text()) if path.exists() else None

    def venv_dists(self) -> dict[str, str]:
        py = self.fp / "venv" / "bin" / "python"
        out = subprocess.run(
            [str(py), "-c", "import json, importlib.metadata as m; "
             "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"],
            capture_output=True, text=True, cwd="/", check=True,
        ).stdout  # fmt: skip
        dists: dict[str, str] = json.loads(out)
        return dists

    def site(self) -> Path:
        return next((self.fp / "venv").glob("lib/python*/site-packages"))


@pytest.fixture
def ghost(wheelhouse: Path, tool_path: str) -> Iterator[GpuHost]:
    root = Path(tempfile.mkdtemp(prefix="nwg", dir="/tmp"))  # short: socket path
    h = GpuHost(root, wheelhouse, tool_path)
    yield h
    h.cleanup()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def up(ghost: GpuHost, base_python: Path) -> Path:
    """A serving worker (stock DGX OS Python), GPU support not run yet."""
    py = ghost.shim(base_python, no_ensurepip=True, pep668=True)
    ghost.status(ghost.run(py))
    return py


def running_job(host: FakeHost) -> subprocess.Popen[bytes]:
    job = host.fp / "jobs" / "j1"
    sleeper = fake_wrapper(job)
    job.mkdir(parents=True)
    (job / "manifest.json").write_text("{}")
    (job / "status.json").write_text(
        json.dumps({"job_id": "j1", "state": "running", "wrapper_pid": sleeper.pid})
    )
    return sleeper


def age(host: GpuHost, seconds: float) -> None:
    state = host.gpu_state() or {}
    state["checked_at"] = time.time() - seconds
    (host.fp / "gpu.json").write_text(json.dumps(state))


def without(wheelhouse: Path, dest: Path, *prefixes: str) -> Path:
    dest.mkdir()
    for wheel in wheelhouse.iterdir():
        if not wheel.name.startswith(prefixes):
            shutil.copy(wheel, dest)
    return dest


# -- the normal path -------------------------------------------------------------


@pytest.mark.parametrize("installer", ["venv-pip", "system-pip", "system-pip-target", "uv"])
def test_gb10_gets_verified_cupy_through_every_installer(
    ghost: GpuHost, base_python: Path, empty_wheelhouse: Path, installer: str
) -> None:
    if installer == "uv":
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv not installed")
        (ghost.home / ".local" / "bin").mkdir(parents=True)
        (ghost.home / ".local" / "bin" / "uv").symlink_to(uv)
    ghost.driver()
    py = ghost.shim(base_python, **INSTALLER_CASES[installer])
    st = ghost.status(ghost.run(py))
    assert ghost.gpu_state() is None  # the worker bootstrap itself never touches the GPU

    state = ghost.gpu_ok(py)
    assert state["ok"] is True, state
    assert (state["dist"], state["cupy"], state["driver"]) == (
        "cupy-cuda13x", FAKE_CUPY, FAKE_DRIVER,
    )  # fmt: skip
    assert state["toolkit"] == "cuda13" and state["unified_memory"] is True
    assert state["smoke"]["device"] == "NVIDIA GB10" and state["smoke"]["compute_cap"] == "12.1"
    dists = ghost.venv_dists()
    assert dists["cupy-cuda13x"] == FAKE_CUPY and "cuda-toolkit" not in dists  # no fallback
    assert dists["numpy"] == FAKE_NUMPY
    assert ghost.worker_info()["pid"] == st["pid"]  # served throughout, never restarted

    # Later runs (reconnects, upgrades) neither reinstall nor re-test a verified GPU...
    ghost.offline(empty_wheelhouse)
    assert ghost.gpu_ok(py)["checked_at"] == state["checked_at"]
    # ...but the user's explicit request always re-runs the smoke test.
    assert ghost.gpu_ok(py, "cuda")["checked_at"] > state["checked_at"]


def test_missing_cuda_libraries_come_from_pypi(ghost: GpuHost, up: Path) -> None:
    ghost.driver()
    ghost.fake_gpu("nvrtc")  # only the driver: no usable system toolkit
    state = ghost.gpu_ok(up)
    assert state["ok"] is True and state["fallback_tried"] is True, state
    dists = ghost.venv_dists()
    assert dists["cuda-toolkit"] == "13.0.2"  # the driver's toolkit, not 13.4
    assert dists["numpy"] == FAKE_NUMPY  # the toolkit wheel alone would pull numpy 2.6


@pytest.mark.parametrize("mode", ["banner", "pathfinder"])
def test_real_cupy_error_shapes_are_understood(ghost: GpuHost, up: Path, mode: str) -> None:
    """CuPy wraps import failures in a "=====" banner; cuda-pathfinder appends
    directory listings. The PyPI toolkit must still be tried for both."""
    ghost.driver()
    ghost.fake_gpu(mode)
    state = ghost.gpu_ok(up)
    assert state["ok"] is True and state["fallback_tried"] is True, state


def test_unfixable_library_error_is_installed_for_once(
    ghost: GpuHost, up: Path, wheelhouse: Path, tmp_path: Path
) -> None:
    # The PyPI toolkit can't be had: say what is missing, and don't keep retrying.
    ghost.driver()
    ghost.fake_gpu("pathfinder")
    ghost.offline(without(wheelhouse, tmp_path / "wheels", "cuda_toolkit"))
    first = ghost.gpu_ok(up)
    assert first["ok"] is False, first
    assert 'Failure finding "libnvrtc.so.13"' in first["cause"]  # not "    stubs"
    ghost.gpu_ok(up, "cuda")  # failed again; from now on it is known
    retried = ghost.gpu(up, "cuda")
    assert retried.returncode == 0 and "FALLBACK" not in retried.stdout
    assert "installing" not in retried.stderr


def test_driver_below_r580_gets_cuda12(ghost: GpuHost, up: Path) -> None:
    ghost.driver("575.57.08")
    state = ghost.gpu_ok(up)
    assert state["ok"] and state["dist"] == "cupy-cuda12x", state
    assert "cupy-cuda13x" not in ghost.venv_dists()


def test_wrong_cuda_major_is_replaced(ghost: GpuHost, up: Path) -> None:
    ghost.driver()
    assert ghost.gpu_ok(up)["dist"] == "cupy-cuda13x"
    ghost.driver("570.172.08")  # driver downgraded below R580: CUDA 13 can't run
    state = ghost.gpu_ok(up, "cuda")
    assert state["ok"] and state["dist"] == "cupy-cuda12x", state
    assert "cupy-cuda13x" not in ghost.venv_dists()


def test_new_driver_or_cupy_is_reverified(ghost: GpuHost, up: Path, wheelhouse: Path) -> None:
    ghost.driver()
    first = ghost.gpu_ok(up)
    ghost.driver("580.105.08")  # OTA update + reboot
    assert ghost.gpu_ok(up)["driver"] == "580.105.08"
    venv_py = ghost.fp / "venv" / "bin" / "python"
    pip_install_into(up, venv_py, wheelhouse, f"cupy-cuda13x=={OLD_CUPY}")  # user downgrades
    again = ghost.gpu_ok(up)
    assert again["cupy"] == OLD_CUPY and again["ok"] and again["checked_at"] > first["checked_at"]


# -- nothing to do ---------------------------------------------------------------


def test_no_driver_installs_nothing(ghost: GpuHost, up: Path) -> None:
    proc = ghost.gpu(up)
    assert proc.returncode == 0 and proc.stdout.strip().splitlines()[-1] == "null"
    assert ghost.gpu_state() is None and "cupy-cuda13x" not in ghost.venv_dists()


def test_gpu_only_needs_an_installed_worker(ghost: GpuHost, base_python: Path) -> None:
    ghost.driver()
    proc = ghost.gpu(ghost.shim(base_python))
    assert proc.returncode == 64 and "bootstrap the worker first" in proc.stderr


# -- failures: recorded, explained, and the worker still serves --------------------


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("sigbus", "crashed (SIGBUS)"),
        ("hang", "hung for 3 s"),
        ("wrong", "wrong results"),
        ("import-error", "ImportError: libcuda.so.1"),
    ],
)
def test_gpu_failures_are_recorded_not_fatal(
    ghost: GpuHost, up: Path, mode: str, message: str
) -> None:
    ghost.driver()
    ghost.fake_gpu(mode)
    proc = ghost.gpu(up, NEWTON_GPU_SMOKE_TIMEOUT="3")
    assert proc.returncode == 0 and "GPU support is not usable" in proc.stderr
    state = ghost.gpu_state()
    assert state is not None and state["ok"] is False and message in state["error"], state
    assert "fallback_tried" not in state  # neither a crash nor the driver's lib: no PyPI toolkit
    assert isinstance(state["log"], list)  # what the child said, for diagnosis
    assert ghost.worker_info()["running"] is True


def test_auto_retries_a_failed_smoke_daily_but_a_request_now(ghost: GpuHost, up: Path) -> None:
    ghost.driver()
    ghost.fake_gpu("wrong")
    failed = ghost.gpu_ok(up)
    assert failed["ok"] is False
    assert ghost.gpu_ok(up) == failed  # an automatic run the same day: no retry

    ghost.fake_gpu("ok")  # e.g. the user fixed something, then pressed the button
    assert ghost.gpu_ok(up, "cuda")["ok"] is True


def test_auto_retries_a_failed_download_daily(
    ghost: GpuHost, up: Path, wheelhouse: Path, tmp_path: Path
) -> None:
    ghost.driver()
    ghost.offline(without(wheelhouse, tmp_path / "wheels", "cupy", "cuda"))
    failed = ghost.gpu_ok(up)
    assert "installing cupy-cuda13x" in failed["error"] and "internet access" in failed["error"]
    assert ghost.gpu_ok(up)["checked_at"] == failed["checked_at"]  # no re-download today
    age(ghost, 2 * 24 * 3600)
    ghost.offline(wheelhouse)
    assert ghost.gpu_ok(up)["ok"] is True  # a day later it tries again


def test_transient_nvidia_smi_failure_keeps_a_verified_gpu(ghost: GpuHost, up: Path) -> None:
    ghost.driver()
    verified = ghost.gpu_ok(up)
    ghost.driver(fail="Unable to determine the device handle for GPU0000:01:00.0: Unknown Error")
    assert ghost.gpu_ok(up) == verified  # a hiccup must not throw away a passed smoke test


def test_broken_driver_is_reported(ghost: GpuHost, up: Path) -> None:
    ghost.driver(fail="Failed to initialize NVML: Driver/library version mismatch")
    state = ghost.gpu_ok(up)
    assert "Driver/library version mismatch" in state["error"]
    assert "cupy-cuda13x" not in ghost.venv_dists()


def test_preconditions_are_rechecked_every_run(ghost: GpuHost, up: Path) -> None:
    ghost.driver("470.256.02")
    assert "too old" in ghost.gpu_ok(up)["error"]
    ghost.driver()  # the driver gets updated
    assert "installs are disabled" in ghost.gpu_ok(up, "auto", "--no-deps")["error"]
    # Installs enabled again: install right away, not "failed today, wait a day".
    assert ghost.gpu_ok(up)["ok"] is True


def test_installs_disabled_also_block_the_toolkit_fallback(
    ghost: GpuHost, up: Path, wheelhouse: Path
) -> None:
    ghost.driver()
    venv_py = ghost.fp / "venv" / "bin" / "python"
    pip_install_into(up, venv_py, wheelhouse, "cupy-cuda13x")  # the user's own CuPy
    ghost.fake_gpu("nvrtc")
    state = ghost.gpu_ok(up, "auto", "--no-deps")
    assert state["ok"] is False and "libnvrtc" in state["error"]
    assert "cuda-toolkit" not in ghost.venv_dists()


def test_target_installs_leave_one_metadata_dir(
    ghost: GpuHost, base_python: Path, wheelhouse: Path
) -> None:
    # Ubuntu 22.04 style (pip-less venv, pip < 22.3): `pip --target --upgrade`
    # never uninstalls, so stale metadata must be cleaned up by hand.
    ghost.driver()
    py = ghost.shim(base_python, **INSTALLER_CASES["system-pip-target"])
    ghost.status(ghost.run(py))
    subprocess.run(
        [str(base_python), "-m", "pip", "install", "-q", "--no-index", "--find-links",
         str(wheelhouse), "--target", str(ghost.site()), "--no-deps", f"cupy-cuda13x=={OLD_CUPY}"],
        check=True,
    )  # fmt: skip
    ghost.fake_gpu("nvrtc")  # the fallback upgrades CuPy through --target --upgrade
    state = ghost.gpu_ok(py)
    assert state["ok"] and state["cupy"] == FAKE_CUPY, state
    assert [d.name for d in ghost.site().glob("cupy_cuda13x-*.dist-info")] == [
        f"cupy_cuda13x-{FAKE_CUPY}.dist-info"
    ]
    assert ghost.venv_dists()["numpy"] == FAKE_NUMPY


# -- running jobs ------------------------------------------------------------------


def test_running_jobs_defer_the_install(ghost: GpuHost, up: Path) -> None:
    ghost.driver()
    sleeper = running_job(ghost)
    try:
        deferred = ghost.gpu_ok(up)  # auto: say so and wait
        assert deferred["deferred"] is True and "cupy-cuda13x" not in ghost.venv_dists()
        explicit = ghost.gpu(up, "cuda")  # the button: tell the user why, change nothing
        assert explicit.returncode == 75, explicit.stderr
        assert "jobs are running" in explicit.stderr
    finally:
        sleeper.kill()
        sleeper.wait()
    assert ghost.gpu_ok(up)["ok"] is True  # deferred, so the next run tries at once


# -- the worker side ---------------------------------------------------------------


def _empty_bundle() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


def test_cuda_jobs_get_the_cache_and_a_memory_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CUPY_GPU_MEMORY_LIMIT", raising=False)
    (tmp_path / "gpu.json").write_text(json.dumps({"ok": True, "unified_memory": True}))
    seen: dict[str, str] = {}

    class Recorder:
        def __init__(self, argv: list[str], env: dict[str, str], **_: Any) -> None:
            seen.update(env)
            self.pid = 4242

    from newton_worker import services

    # No model services here: the usual cap (the beside-services cap has its own tests).
    monkeypatch.setattr(services, "memory_now", lambda: {"total": None, "available": None})
    monkeypatch.setattr(worker_jobs.subprocess, "Popen", Recorder)
    store = worker_jobs.JobStore(tmp_path)
    store.create({"job_id": "j-1", "command": ["python", "-m", "newton_worker.selftest"]})
    store.put_bundle("j-1", _empty_bundle())
    store.start("j-1")
    assert seen["CUPY_CACHE_DIR"] == str(tmp_path / "cache" / "cupy")
    assert seen["CUPY_GPU_MEMORY_LIMIT"] == "50%"
    # ...and the job wrapper hands both on to the benchmark process.
    monkeypatch.setattr(run_job.os, "environ", {**seen, "PATH": "/usr/bin"})
    env = run_job.job_env({"job_id": "j-1", "backend": "cuda"}, tmp_path)
    assert env["CUPY_GPU_MEMORY_LIMIT"] == "50%" and env["CUPY_CACHE_DIR"] == seen["CUPY_CACHE_DIR"]

    monkeypatch.setattr(gpu.os, "environ", {"CUPY_GPU_MEMORY_LIMIT": "80%"})
    assert "CUPY_GPU_MEMORY_LIMIT" not in gpu.job_env(tmp_path)  # the owner's setting wins


def test_preflight_refuses_cuda_after_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_jobs.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(worker_jobs, "_installed", lambda dist: dist == "cupy-cuda13x")
    monkeypatch.setattr(gpu, "installed_cupy", lambda: ("cupy-cuda13x", "14.2.0"))
    monkeypatch.setattr(gpu, "driver_version", lambda: ("580.95.05", None))
    assert worker_jobs.backend_unavailable("cuda", tmp_path) is None  # never checked: try
    failed = {"ok": False, "cupy": "14.2.0", "driver": "580.95.05", "error": "crashed (SIGBUS)"}
    (tmp_path / "gpu.json").write_text(json.dumps(failed))
    assert "SIGBUS" in (worker_jobs.backend_unavailable("cuda", tmp_path) or "")
    for stale in ({"cupy": "14.0.1"}, {"driver": "575.57.08"}):  # since replaced: let it try
        (tmp_path / "gpu.json").write_text(json.dumps({**failed, **stale}))
        assert worker_jobs.backend_unavailable("cuda", tmp_path) is None


def test_plan_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "driver_version", lambda: ("580.95.05", None))
    monkeypatch.setattr(gpu, "installed_cupy", lambda: (None, None))
    assert gpu.plan(tmp_path, "auto", can_install=True) == ["install", "cupy-cuda13x>=14.0.1,<15"]
    monkeypatch.setattr(gpu, "MIN_PYTHON", {"cuda13": (99, 0), "cuda12": (3, 9)})
    assert gpu.plan(tmp_path, "auto", can_install=True)[0] == "skip"
    assert "needs Python >= 99.0" in (gpu.read_state(tmp_path) or {})["error"]
    monkeypatch.setattr(gpu, "driver_version", lambda: ("575.57.08", None))
    assert gpu.plan(tmp_path, "auto", can_install=True)[0] == "install"  # 3.9+ is fine for 12


def test_signal_exit_codes() -> None:
    bus = 7 if sys.platform == "linux" else 10
    assert gpu._signal_name(-bus) == "SIGBUS" and gpu._signal_name(128 + bus) == "SIGBUS"
    assert gpu._signal_name(1) is None and gpu._signal_name(0) is None


def test_toolkit_fallback_only_for_library_errors_on_our_own_cupy() -> None:
    base = {"ok": False, "toolkit": "cuda13", "dist": "cupy-cuda13x"}
    missing = {**base, "error": "CompileException: libnvrtc.so.13: cannot open shared object"}
    assert gpu.toolkit_fallback(missing) == gpu.TOOLKIT_FALLBACK["cuda13"]
    assert gpu.toolkit_fallback({**missing, "fallback_tried": True}) is None
    assert gpu.toolkit_fallback({**missing, "dist": "cupy-cuda12x"}) is None
    driver_lib = {**base, "error": "ImportError: libcuda.so.1: cannot open shared object file"}
    assert gpu.toolkit_fallback(driver_lib) is None  # the driver's library: PyPI can't help
    assert gpu.toolkit_fallback({**base, "error": "the CUDA smoke test crashed (SIGBUS)"}) is None
    insufficient = {**base, "error": "cudaErrorInsufficientDriver: CUDA driver version is "
                    "insufficient for CUDA runtime version"}  # fmt: skip
    assert gpu.toolkit_fallback(insufficient) is None  # what real CuPy says with no GPU


def test_error_summary_picks_the_cause() -> None:
    banner = (
        "\n=====\nFailed to import CuPy.\n\nOriginal error:\n"
        "  ImportError: libcublas.so.13: cannot open shared object file\n=====\n"
    )
    assert gpu._summary(banner) == "ImportError: libcublas.so.13: cannot open shared object file"
    listing = 'Failure finding "libnvrtc.so.13": nope\n  listdir("/x"):\n    stubs'
    assert gpu._summary(listing).startswith('Failure finding "libnvrtc.so.13"')
