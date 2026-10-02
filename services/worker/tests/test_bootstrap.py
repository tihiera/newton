"""bootstrap.sh against simulated distro Pythons, run with dash (Ubuntu's sh).

Every scenario must end in one of two states: a running worker whose venv
imports numpy 2.0–2.5, or a specific non-zero exit that left the host's
previous src, venv and worker untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pyenv_shims import (  # noqa: F401 - fixtures
    FAKE_MATPLOTLIB,
    FAKE_NUMPY,
    OLD_NUMPY,
    base_python,
    empty_wheelhouse,
    find_dash,
    make_python_shim,
    offline_index_env,
    pip_install_into,
    tool_path,
    wheelhouse,
)

WORKER_DIR = Path(__file__).resolve().parents[1]
SHELL = find_dash()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def copy_worker(dest: Path) -> None:
    shutil.copytree(
        WORKER_DIR / "newton_worker",
        dest / "newton_worker",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(WORKER_DIR / "bootstrap.sh", dest / "bootstrap.sh")


class FakeHost:
    def __init__(self, root: Path, wheelhouse: Path, tool_path: str) -> None:
        self.root = root
        self.home = root / "home"
        self.fp = self.home / ".newton"
        self.wheelhouse = wheelhouse
        copy_worker(self.fp / "src")
        (self.fp / "token").write_text("t0ken")
        self.env = {
            "HOME": str(self.home),
            "PATH": tool_path,
            # Lock regressions must fail in seconds, not after the 300 s default.
            "NEWTON_BOOTSTRAP_LOCK_WAIT": "2",
            **offline_index_env(wheelhouse, root / "uv-cache"),
        }
        self.pids: set[int] = set()

    def shim(self, base: Path, **modes: bool) -> Path:
        return make_python_shim(self.root / "bin" / "python3", base, **modes)

    def offline(self, wheelhouse: Path) -> None:
        self.env.update(PIP_FIND_LINKS=str(wheelhouse), UV_FIND_LINKS=str(wheelhouse))

    def run(
        self, python: Path | str, *flags: str, script: Path | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        script = script or self.fp / "src" / "bootstrap.sh"
        argv = [SHELL, str(script), "--python", str(python), *flags]
        # sshd starts commands in $HOME: so do we (files there must not shadow modules).
        proc = subprocess.run(
            argv,
            env={**self.env, **env},
            cwd=self.home,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0:
            self.pids.add(self.status(proc)["pid"])
        return proc

    @staticmethod
    def status(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        assert proc.returncode == 0, proc.stderr
        data: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
        return data

    def worker_info(self) -> dict[str, Any]:
        out = subprocess.run(
            [sys.executable, "-m", "newton_worker", "--root", str(self.fp), "status"],
            env={**os.environ, "PYTHONPATH": str(self.fp / "src")},
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        info: dict[str, Any] = json.loads(out)
        return info

    def venv_imports_numpy(self) -> str | None:
        py = self.fp / "venv" / "bin" / "python"
        out = subprocess.run(
            [str(py), "-c", "import numpy; print(numpy.__version__)"],
            capture_output=True,
            text=True,
            cwd="/",
        )
        return out.stdout.strip() if out.returncode == 0 else None

    def cleanup(self) -> None:
        if (self.fp / "src").exists():
            subprocess.run(
                [sys.executable, "-m", "newton_worker", "--root", str(self.fp), "stop"],
                env={**os.environ, "PYTHONPATH": str(self.fp / "src")},
                capture_output=True,
                timeout=60,
            )
        for pid in self.pids:
            if alive(pid):
                os.kill(pid, 9)


@pytest.fixture
def host(wheelhouse: Path, tool_path: str) -> Iterator[FakeHost]:
    # Short root: the worker's Unix socket path must fit in ~104 bytes.
    root = Path(tempfile.mkdtemp(prefix="nw", dir="/tmp"))
    h = FakeHost(root, wheelhouse, tool_path)
    yield h
    h.cleanup()
    shutil.rmtree(root, ignore_errors=True)


def fake_wrapper(job_dir: Path) -> subprocess.Popen[bytes]:
    """A long-running process whose command line looks like a job wrapper."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "newton_worker.run_job", str(job_dir)]
    )


def assert_healthy(st: dict[str, Any], installer: str) -> None:
    assert st["running"] is True
    assert st["deps"] == {"numpy": FAKE_NUMPY, "matplotlib": FAKE_MATPLOTLIB}
    assert st["bootstrap"]["installer"] == installer


# -- the installer chain --------------------------------------------------------

INSTALLER_CASES = {
    "venv-pip": {},
    "system-pip": {"no_ensurepip": True, "pep668": True},  # DGX OS / Ubuntu 24.04
    "system-pip-target": {"no_ensurepip": True, "old_pip": True},  # Ubuntu 22.04
    "uv": {"no_venv": True, "no_pip": True},
}


@pytest.mark.parametrize("installer", INSTALLER_CASES)
def test_installer_chain_then_offline_restart(
    host: FakeHost, base_python: Path, empty_wheelhouse: Path, installer: str
) -> None:
    if installer == "uv":
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv not installed")
        (host.home / ".local" / "bin").mkdir(parents=True)
        (host.home / ".local" / "bin" / "uv").symlink_to(uv)
    py = host.shim(base_python, **INSTALLER_CASES[installer])
    first = host.status(host.run(py))
    assert_healthy(first, installer)
    assert first["bootstrap"]["venv"] == "created"
    assert "include-system-site-packages = false" in (host.fp / "venv/pyvenv.cfg").read_text()

    # A reboot or upgrade restart must not need the package index again.
    host.offline(empty_wheelhouse)
    second = host.status(host.run(py))
    assert_healthy(second, "satisfied")
    assert second["bootstrap"]["venv"] == "reused"
    assert second["pid"] != first["pid"]
    assert not alive(first["pid"])  # the old worker was really stopped
    assert not (host.fp / "bootstrap.lock").exists()


@pytest.mark.parametrize("installer", ["venv-pip", "system-pip", "system-pip-target"])
def test_old_numpy_in_reused_venv_is_upgraded(
    host: FakeHost, base_python: Path, wheelhouse: Path, installer: str
) -> None:
    # A reused venv holding apt-era numpy 1.26 must end up on the pinned 2.x.
    venv = host.fp / "venv"
    flags = [] if installer == "venv-pip" else ["--without-pip"]
    subprocess.run([str(base_python), "-m", "venv", *flags, str(venv)], check=True)
    pip_install_into(base_python, venv / "bin/python", wheelhouse, f"numpy=={OLD_NUMPY}")
    st = host.status(host.run(host.shim(base_python, **INSTALLER_CASES[installer])))
    assert st["bootstrap"]["venv"] == "reused"
    assert st["bootstrap"]["installer"] == installer
    assert st["deps"]["numpy"] == FAKE_NUMPY  # not 1.26.4, not the out-of-range 2.6.0
    site = next(venv.glob("lib/python*/site-packages"))
    assert sorted(d.name for d in site.glob("numpy-*.dist-info")) == [
        f"numpy-{FAKE_NUMPY}.dist-info"
    ]  # metadata matches what imports


def test_old_numpy_with_installs_disabled_is_refused(
    host: FakeHost, base_python: Path, wheelhouse: Path
) -> None:
    venv = host.fp / "venv"
    subprocess.run([str(base_python), "-m", "venv", str(venv)], check=True)
    pip_install_into(base_python, venv / "bin/python", wheelhouse, f"numpy=={OLD_NUMPY}")
    proc = host.run(host.shim(base_python), "--no-deps")
    assert proc.returncode == 68, proc.stderr
    assert "outside 2.0" in proc.stderr
    assert not (host.fp / "worker.json").exists()


def test_jobs_running_defer_a_numpy_change_in_place(
    host: FakeHost, base_python: Path, wheelhouse: Path
) -> None:
    venv = host.fp / "venv"
    subprocess.run([str(base_python), "-m", "venv", str(venv)], check=True)
    pip_install_into(base_python, venv / "bin/python", wheelhouse, f"numpy=={OLD_NUMPY}")
    job = host.fp / "jobs" / "j1"
    sleeper = fake_wrapper(job)
    try:
        job.mkdir(parents=True)
        (job / "manifest.json").write_text("{}")
        (job / "status.json").write_text(
            json.dumps({"job_id": "j1", "state": "running", "wrapper_pid": sleeper.pid})
        )
        proc = host.run(host.shim(base_python))
        assert proc.returncode == 75, proc.stderr
        assert host.venv_imports_numpy() == OLD_NUMPY  # untouched under the running job
    finally:
        sleeper.kill()
        sleeper.wait()


def test_install_stage_without_any_installer_exits_69(host: FakeHost, base_python: Path) -> None:
    # The likely stock DGX state: `venv --without-pip` works, but no pip and no uv.
    proc = host.run(host.shim(base_python, no_ensurepip=True, no_pip=True))
    assert proc.returncode == 69, proc.stderr
    assert "sudo apt install python3-venv" in proc.stderr
    assert not (host.fp / "worker.json").exists()
    assert not (host.fp / "venv").exists() and not (host.fp / "venv.new").exists()


def test_no_way_to_build_a_venv_exits_69(host: FakeHost, base_python: Path) -> None:
    proc = host.run(host.shim(base_python, no_venv=True, no_pip=True))
    assert proc.returncode == 69, proc.stderr
    assert not (host.fp / "worker.json").exists()
    assert not (host.fp / "bootstrap.lock").exists()


def test_failed_install_exits_68(host: FakeHost, base_python: Path, empty_wheelhouse: Path) -> None:
    host.offline(empty_wheelhouse)
    proc = host.run(host.shim(base_python))
    assert proc.returncode == 68, proc.stderr
    assert "PIP_INDEX_URL" in proc.stderr
    assert not (host.fp / "worker.json").exists()


def test_matplotlib_failure_does_not_block_numpy(
    host: FakeHost, base_python: Path, tmp_path: Path
) -> None:
    only_numpy = tmp_path / "only-numpy"
    only_numpy.mkdir()
    for wheel in host.wheelhouse.glob("numpy-*.whl"):
        shutil.copy(wheel, only_numpy)
    host.offline(only_numpy)
    proc = host.run(host.shim(base_python))
    st = host.status(proc)
    assert st["deps"] == {"numpy": FAKE_NUMPY, "matplotlib": None}
    assert "matplotlib" in proc.stderr


# -- never touch the running host on failure ---------------------------------------


def test_failed_rebuild_keeps_old_venv_and_worker(
    host: FakeHost, base_python: Path, empty_wheelhouse: Path
) -> None:
    py = host.shim(base_python)
    first = host.status(host.run(py))
    cfg = host.fp / "venv" / "pyvenv.cfg"
    cfg.write_text(cfg.read_text().replace("= false", "= true"))  # legacy system-site venv
    host.offline(empty_wheelhouse)
    proc = host.run(py)
    assert proc.returncode == 68, proc.stderr
    assert host.venv_imports_numpy() == FAKE_NUMPY  # old venv intact
    assert not (host.fp / "venv.new").exists()
    info = host.worker_info()
    assert info["running"] and info["pid"] == first["pid"]  # old worker untouched


def test_jobs_running_defer_venv_rebuild(host: FakeHost, base_python: Path) -> None:
    py = host.shim(base_python)
    host.status(host.run(py))
    job = host.fp / "jobs" / "j1"
    sleeper = fake_wrapper(job)
    try:
        job.mkdir(parents=True)
        (job / "manifest.json").write_text("{}")
        (job / "status.json").write_text(
            json.dumps({"job_id": "j1", "state": "running", "wrapper_pid": sleeper.pid})
        )
        cfg = host.fp / "venv" / "pyvenv.cfg"
        cfg.write_text(cfg.read_text().replace("= false", "= true"))
        proc = host.run(py)
        assert proc.returncode == 75, proc.stderr
        assert "jobs are running" in proc.stderr
    finally:
        sleeper.kill()
        sleeper.wait()
    st = host.status(host.run(py))  # wrapper gone: the job is lost, rebuild proceeds
    assert st["bootstrap"]["venv"] == "created"


def test_staged_source_is_swapped_only_on_success(host: FakeHost, base_python: Path) -> None:
    py = host.shim(base_python)
    host.status(host.run(py))
    (host.fp / "src" / "MARKER").write_text("old")

    bad = host.fp / "src.stage-0123456789abcdef"
    copy_worker(bad)
    proc = host.run("/nonexistent/python3", "--stage", bad.name, script=bad / "bootstrap.sh")
    assert proc.returncode == 66
    assert (host.fp / "src" / "MARKER").read_text() == "old"  # live source untouched
    assert not bad.exists()  # a failed bootstrap cleans up its own staging dir

    good = host.fp / "src.stage-fedcba9876543210"
    copy_worker(good)
    host.status(host.run(py, "--stage", good.name, script=good / "bootstrap.sh"))
    assert not (host.fp / "src" / "MARKER").exists()  # swapped in
    assert not good.exists()


# -- environment hygiene -----------------------------------------------------------


def test_login_pythonpath_cannot_fake_numpy(
    host: FakeHost, base_python: Path, wheelhouse: Path, tmp_path: Path
) -> None:
    leaked = tmp_path / "leaked-site"
    subprocess.run(
        [str(base_python), "-m", "pip", "install", "--quiet", "--no-index", "--find-links",
         str(wheelhouse), "--target", str(leaked), f"numpy=={OLD_NUMPY}"],
        check=True,
    )  # fmt: skip
    st = host.status(host.run(host.shim(base_python), PYTHONPATH=str(leaked)))
    assert_healthy(st, "venv-pip")
    assert host.venv_imports_numpy() == FAKE_NUMPY


def _old_system_python() -> str | None:
    """A Python < 3.11 (ignores PYTHONSAFEPATH, like Ubuntu 22.04's), if present."""
    candidate = "/usr/bin/python3"
    if not os.path.exists(candidate):
        return None
    out = subprocess.run(
        [candidate, "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
        capture_output=True,
        text=True,
    )
    return candidate if out.stdout.strip() == "True" else None


@pytest.mark.parametrize("interpreter", ["base", "old-system"])
def test_home_directory_files_do_not_shadow_modules(
    host: FakeHost, base_python: Path, interpreter: str
) -> None:
    if interpreter == "base":
        py: Path | str = host.shim(base_python)
    else:
        old = _old_system_python()
        if old is None:
            pytest.skip("no Python < 3.11 on this machine")
        py = old  # there `cd /` is the only defence
    (host.home / "random.py").write_text("raise SystemExit('shadowed stdlib random')\n")
    (host.home / "numpy").mkdir()
    (host.home / "numpy" / "__init__.py").write_text("raise ImportError('shadowed numpy')\n")
    st = host.status(host.run(py))
    assert st["running"] and st["deps"]["numpy"] == FAKE_NUMPY


def test_venv_from_another_python_is_rebuilt(host: FakeHost, base_python: Path) -> None:
    py = host.shim(base_python)
    host.status(host.run(py))
    cfg = host.fp / "venv" / "pyvenv.cfg"
    cfg.write_text(cfg.read_text().replace("version = ", "version = 3.8.0 # was "))
    st = host.status(host.run(py))
    assert st["bootstrap"]["venv"] == "created"


def test_permissions_are_private(host: FakeHost) -> None:
    host.status(host.run(sys.executable, "--no-venv"))
    assert (host.fp.stat().st_mode & 0o777) == 0o700
    assert (host.fp / "token").stat().st_mode & 0o777 == 0o600
    assert (host.fp / "bootstrap.json").stat().st_mode & 0o077 == 0


# -- --no-venv / --no-deps -----------------------------------------------------------


def test_no_venv_never_installs_into_system_python(host: FakeHost, base_python: Path) -> None:
    proc = host.run(host.shim(base_python, pep668=True), "--no-venv")
    assert proc.returncode == 68, proc.stderr
    assert "never installs into a system" in proc.stderr
    assert not (host.fp / "worker.json").exists()


def test_no_venv_with_numpy_already_present(host: FakeHost) -> None:
    st = host.status(host.run(sys.executable, "--no-venv"))
    assert st["running"] is True
    assert st["deps"]["numpy"] is not None
    assert st["bootstrap"]["venv"] == "none" and st["bootstrap"]["installer"] == "skipped"


def test_no_deps_respects_the_flag(
    host: FakeHost, base_python: Path, empty_wheelhouse: Path
) -> None:
    py = host.shim(base_python)
    proc = host.run(py, "--no-deps")
    assert proc.returncode == 68, proc.stderr
    assert "dependency install is disabled" in proc.stderr
    assert host.status(host.run(py))["deps"]["numpy"] == FAKE_NUMPY
    host.offline(empty_wheelhouse)
    st = host.status(host.run(py, "--no-deps"))
    assert st["bootstrap"]["installer"] == "skipped"


# -- locking and process safety ---------------------------------------------------------


def test_lock_held_by_live_bootstrap_exits_75(host: FakeHost) -> None:
    lock = host.fp / "bootstrap.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))  # a live owner
    started = time.time()
    proc = host.run(sys.executable, "--no-venv", NEWTON_BOOTSTRAP_LOCK_WAIT="1")
    assert proc.returncode == 75, proc.stderr
    assert time.time() - started < 30
    assert lock.exists()  # not ours to remove


def test_lock_of_dead_or_ancient_owner_is_taken_over(host: FakeHost) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = host.fp / "bootstrap.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(dead.pid))
    assert host.run(sys.executable, "--no-venv").returncode == 0

    lock.mkdir()
    old = time.time() - 2 * 3600
    os.utime(lock, (old, old))
    assert host.run(sys.executable, "--no-venv").returncode == 0


def test_concurrent_bootstraps_serialize(host: FakeHost, base_python: Path) -> None:
    py = host.shim(base_python)
    script = str(host.fp / "src/bootstrap.sh")
    argv = [SHELL, script, "--python", str(py)]
    procs = [
        subprocess.Popen(
            argv,
            env={**host.env, "NEWTON_BOOTSTRAP_LOCK_WAIT": "120"},  # waiting is the point
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=300) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], [o[1] for o in outs]
    pids = [json.loads(o[0].strip().splitlines()[-1])["pid"] for o in outs]
    host.pids.update(pids)
    assert sum(alive(pid) for pid in pids) == 1  # exactly one worker survives


def test_stale_pid_in_worker_json_is_not_killed(host: FakeHost) -> None:
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        (host.fp / "worker.json").write_text(json.dumps({"pid": bystander.pid, "port": 1}))
        assert host.run(sys.executable, "--no-venv").returncode == 0
        assert bystander.poll() is None  # still alive
    finally:
        bystander.kill()
        bystander.wait()


# -- argument and environment errors --------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "message"),
    [(("--python",), "--python needs a value"), (("--bogus",), "unknown argument")],
)
def test_bad_arguments_exit_64(host: FakeHost, flags: tuple[str, ...], message: str) -> None:
    proc = subprocess.run(
        [SHELL, str(host.fp / "src" / "bootstrap.sh"), *flags],
        env=host.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 64
    assert message in proc.stderr


def test_missing_token_and_python(host: FakeHost) -> None:
    assert host.run("/nonexistent/python3").returncode == 66
    (host.fp / "token").unlink()
    assert host.run(sys.executable).returncode == 65


def test_socket_path_too_long_is_refused(tmp_path: Path, tool_path: str) -> None:
    home = tmp_path / ("x" * 60)
    copy_worker(home / ".newton" / "src")
    (home / ".newton" / "token").write_text("t")
    proc = subprocess.run(
        [SHELL, str(home / ".newton/src/bootstrap.sh"), "--python", sys.executable, "--no-venv"],
        env={"HOME": str(home), "PATH": tool_path},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 64
    assert "too long for a Unix socket" in proc.stderr


def test_refuses_to_run_outside_a_newton_home(tmp_path: Path, tool_path: str) -> None:
    elsewhere = tmp_path / "opt" / "src"
    copy_worker(elsewhere)
    proc = subprocess.run(
        [SHELL, str(elsewhere / "bootstrap.sh")],
        env={"HOME": str(tmp_path), "PATH": tool_path},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 64
    assert "unexpected install location" in proc.stderr


def test_new_worker_that_cannot_start_is_rolled_back(host: FakeHost, base_python: Path) -> None:
    py = host.shim(base_python)
    first = host.status(host.run(py))
    (host.fp / "src" / "MARKER").write_text("old")

    broken = host.fp / "src.stage-00000000deadbeef"
    copy_worker(broken)
    server = broken / "newton_worker" / "server.py"
    text = server.read_text()
    anchor = "    root.mkdir(parents=True, exist_ok=True)\n    store = JobStore"
    assert anchor in text
    server.write_text(text.replace(anchor, '    raise OSError("broken build")\n' + anchor, 1))

    proc = host.run(py, "--stage", broken.name, script=broken / "bootstrap.sh")
    assert proc.returncode == 76, proc.stderr  # transient: the old version is serving
    assert "restored and restarted the previous worker" in proc.stderr
    assert (host.fp / "src" / "MARKER").read_text() == "old"  # previous source is back
    info = wait_running(host)
    assert info["pid"] != first["pid"]  # the previous code, restarted
    host.pids.add(info["pid"])


def wait_running(host: FakeHost, timeout: float = 20) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = host.worker_info()
        if info.get("running"):
            return info
        time.sleep(0.2)
    raise AssertionError("worker did not come back")


def test_missing_matplotlib_never_blocks_a_restart_while_jobs_run(
    host: FakeHost, base_python: Path, tmp_path: Path
) -> None:
    only_numpy = tmp_path / "only-numpy"
    only_numpy.mkdir()
    for wheel in host.wheelhouse.glob("numpy-*.whl"):
        shutil.copy(wheel, only_numpy)
    host.offline(only_numpy)
    py = host.shim(base_python)
    host.status(host.run(py))  # matplotlib could not be installed
    job = host.fp / "jobs" / "j1"
    sleeper = fake_wrapper(job)
    try:
        job.mkdir(parents=True)
        (job / "manifest.json").write_text("{}")
        (job / "status.json").write_text(
            json.dumps({"job_id": "j1", "state": "running", "wrapper_pid": sleeper.pid})
        )
        proc = host.run(py)  # e.g. restarting a crashed worker mid-job
        st = host.status(proc)
        assert st["running"] and st["deps"]["matplotlib"] is None
        assert "matplotlib install skipped" in proc.stderr
    finally:
        sleeper.kill()
        sleeper.wait()


def test_racing_takeover_of_a_dead_owners_lock(host: FakeHost) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = host.fp / "bootstrap.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(dead.pid))
    argv = [SHELL, str(host.fp / "src/bootstrap.sh"), "--python", sys.executable, "--no-venv"]
    procs = [
        subprocess.Popen(
            argv,
            env={**host.env, "NEWTON_BOOTSTRAP_LOCK_WAIT": "120"},  # waiting is the point
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    outs = [p.communicate(timeout=300) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], [o[1] for o in outs]
    pids = [json.loads(o[0].strip().splitlines()[-1])["pid"] for o in outs]
    host.pids.update(pids)
    assert sum(alive(pid) for pid in pids) == 1
    assert not list(host.fp.glob("bootstrap.lock*"))


def test_exit_trap_never_removes_someone_elses_lock(host: FakeHost, tool_path: str) -> None:
    if shutil.which("flock", path=tool_path):
        pytest.skip("this host uses flock(1), not the mkdir lock")
    import threading

    lock_pid = host.fp / "bootstrap.lock" / "pid"

    def steal() -> None:  # another bootstrap takes over while we run
        deadline = time.time() + 30
        while time.time() < deadline and not lock_pid.exists():
            time.sleep(0.005)
        lock_pid.write_text(str(os.getpid()))

    thief = threading.Thread(target=steal)
    thief.start()
    host.run(sys.executable, "--no-venv")
    thief.join()
    assert lock_pid.exists()  # our EXIT trap left the new owner's lock alone


def test_relative_python_is_relative_to_home(host: FakeHost) -> None:
    (host.home / "tools").mkdir()
    (host.home / "tools" / "python3").symlink_to(sys.executable)
    st = host.status(host.run("tools/python3", "--no-venv"))
    assert st["running"]


def test_reused_wrapper_pid_does_not_count_as_a_running_job(
    host: FakeHost, base_python: Path
) -> None:
    py = host.shim(base_python)
    host.status(host.run(py))
    bystander = subprocess.Popen(["sleep", "60"])  # e.g. after a reboot, pid reused
    try:
        job = host.fp / "jobs" / "j1"
        job.mkdir(parents=True)
        (job / "manifest.json").write_text("{}")
        (job / "status.json").write_text(
            json.dumps({"job_id": "j1", "state": "running", "wrapper_pid": bystander.pid})
        )
        cfg = host.fp / "venv" / "pyvenv.cfg"
        cfg.write_text(cfg.read_text().replace("= false", "= true"))  # needs a rebuild
        st = host.status(host.run(py))  # not wedged at 75 by an unrelated process
        assert st["bootstrap"]["venv"] == "created"
        assert json.loads((job / "status.json").read_text())["state"] == "lost"
    finally:
        bystander.kill()
        bystander.wait()
