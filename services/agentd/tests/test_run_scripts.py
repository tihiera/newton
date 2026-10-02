"""B7: scripts/setup.sh and scripts/run.sh, the way Newton runs from a git clone.

The scripts run against stub programs (uname, sw_vers, uv, node, pnpm, cargo) put first
on PATH, so each test fixes the Mac it describes. setup.sh runs with --dry-run, or with
every program it calls stubbed.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import tomllib

REPO = Path(__file__).resolve().parents[3]
SETUP = REPO / "scripts" / "setup.sh"
RUN = REPO / "scripts" / "run.sh"
SHELLS = ["sh", *(["dash"] if shutil.which("dash") else [])]
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def mac(tmp_path: Path, macos: str = "15.1", arch: str = "arm64") -> tuple[Path, dict[str, str]]:
    """A stub bin folder for a Mac with this macOS and processor, and its environment
    (HOME in tmp_path, so nothing from this machine's ~/.local or ~/.cargo leaks in)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    stub(bin_dir, "uname", f'case "$1" in -m) echo {arch} ;; *) echo Darwin ;; esac')
    stub(bin_dir, "sw_vers", f"echo {macos}")
    stub(bin_dir, "uv", f'echo "$*" >>"{tmp_path}/uv.calls"; echo "uv 0.9.0"')
    env = {"PATH": f"{bin_dir}:{SYSTEM_PATH}", "HOME": str(home), "LANG": "C"}
    return bin_dir, env


def run(argv: list[str], env: dict[str, str], cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


# --- setup.sh ----------------------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
def test_setup_refuses_apple_silicon_before_macos_14_before_any_uv_step(
    tmp_path: Path, shell: str
) -> None:
    """MLX (locked for every Apple Silicon Mac) has no macOS 13 build: uv sync would fail."""
    _, env = mac(tmp_path, macos="13.6.1")
    result = run([shell, str(SETUP), "--dry-run"], env)
    assert result.returncode == 1
    assert "Newton needs macOS 14 or newer on Apple Silicon" in result.stdout
    assert "run scripts/setup.sh again" in result.stdout
    assert "CPU until then" not in result.stdout
    assert not (tmp_path / "uv.calls").exists()


def test_setup_lets_an_intel_mac_on_macos_13_through(tmp_path: Path) -> None:
    bin_dir, env = mac(tmp_path, macos="13.6.1", arch="x86_64")
    stub(bin_dir, "sysctl", "echo 0")
    result = run(["sh", str(SETUP), "--dry-run"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sync --frozen" in result.stdout and "--extra mac" not in result.stdout


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("v18.20.4", False),
        ("v20.11.1", False),
        ("v20.19.0", True),
        ("v21.7.3", False),
        ("v22.11.0", False),
        ("v22.12.0", True),
        ("v23.0.0", True),
        ("v24.1.0", True),
        ("v26.7.0", True),
    ],
)
def test_setup_accepts_only_node_versions_vite_supports(
    tmp_path: Path, version: str, supported: bool
) -> None:
    bin_dir, env = mac(tmp_path)
    stub(bin_dir, "node", f"echo {version}")
    stub(bin_dir, "pnpm", "echo 10.33.0")
    result = run(["sh", str(SETUP), "--dry-run"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    if supported:
        assert f"ok Node.js {version}" in result.stdout
        assert "pnpm --dir apps/desktop install --frozen-lockfile" in result.stdout
    else:
        assert f"Node.js {version} can't run Newton's UI" in result.stdout
        assert "20.19 or newer 20.x, or 22.12 or newer" in result.stdout
        assert "install --frozen-lockfile" not in result.stdout


def test_setup_turns_off_corepacks_download_prompt_before_calling_pnpm(tmp_path: Path) -> None:
    """corepack's pnpm shim asks on the terminal (stderr hidden) unless the prompt is off."""
    bin_dir, env = mac(tmp_path)
    stub(bin_dir, "node", "echo v22.12.0")
    stub(
        bin_dir,
        "pnpm",
        '[ "${COREPACK_ENABLE_DOWNLOAD_PROMPT:-}" = 0 ] || { echo prompted; exit 1; }\n'
        "echo 10.33.0",
    )
    result = run(["sh", str(SETUP), "--dry-run"], env)
    assert "ok pnpm 10.33.0" in result.stdout, result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_help_works_from_any_folder(tmp_path: Path, shell: str) -> None:
    _, env = mac(tmp_path)
    run_help = run([shell, "../../scripts/run.sh", "--help"], env, cwd=REPO / "apps" / "desktop")
    assert run_help.returncode == 0 and run_help.stderr == ""
    assert run_help.stdout.startswith("Start Newton from this clone")
    setup_help = run([shell, "./setup.sh", "--help"], env, cwd=REPO / "scripts")
    assert setup_help.returncode == 0 and setup_help.stderr == ""
    assert setup_help.stdout.startswith("Set Newton up on this Mac")


# --- run.sh --status ---------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def status(env: dict[str, str], cwd: Path, data_dir: str) -> str:
    env = {**env, "NEWTON_DATA_DIR": data_dir, "NEWTON_PORT": str(free_port())}
    result = run(["sh", str(RUN), "--status"], env, cwd=cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize(
    ("given", "expected"),
    [("newton-data", "work/newton-data"), ("~/newton", "home/newton"), ("~", "home")],
)
def test_run_makes_the_data_folder_absolute(tmp_path: Path, given: str, expected: str) -> None:
    """Relative to where run.sh was started (not the repo), with a quoted ~ expanded."""
    _, env = mac(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    out = status(env, work, given)
    assert f"Data folder: {tmp_path / expected}\n" in out


def test_run_finds_pnpm_where_setup_put_it(tmp_path: Path) -> None:
    bin_dir, env = mac(tmp_path)
    stub(bin_dir, "cargo", "echo cargo 1.90.0")
    out = status(env, tmp_path, str(tmp_path / "data"))
    assert "scripts/run.sh opens: the browser UI (no desktop window: pnpm isn't installed)" in out
    local_bin = tmp_path / "home" / ".local" / "bin"
    local_bin.mkdir(parents=True)
    stub(local_bin, "pnpm", "echo 10.33.0")
    out = status(env, tmp_path, str(tmp_path / "data"))
    assert "scripts/run.sh opens: the desktop window" in out


def test_run_without_rust_opens_the_browser(tmp_path: Path) -> None:
    _, env = mac(tmp_path)
    out = status(env, tmp_path, str(tmp_path / "data"))
    assert "opens: the browser UI (no desktop window: Rust (cargo) isn't installed)" in out


# --- run.sh: closing the terminal stops everything --------------------------------

FAKE_AGENTD = """
import http.server, json, os, sys
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
data = args[args.index("--data-dir") + 1]
with open(os.path.join(data, "fake-agentd.pid"), "w") as f:
    f.write(str(os.getpid()))
with open(os.path.join(data, "api-token"), "w") as f:
    f.write("test-token")

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "db": {"path": os.path.join(data, "newton.db")}})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""

# vite with a child in its process group, which only a signal to the group stops.
FAKE_VITE = """
import http.server, os, signal, sys, time
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
child = os.fork()
if child == 0:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    while True:
        time.sleep(1)
with open(os.environ["FAKE_VITE_PIDS"], "w") as f:
    f.write(f"{os.getpid()} {child}")

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


def listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def ui_port_busy() -> bool:
    """Whether something (a vite dev server listens on ::1) holds the UI's port 1420."""
    if listening(1420):
        return True
    with socket.socket(socket.AF_INET6) as s:
        return s.connect_ex(("::1", 1420)) == 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stops(check: Callable[[int], bool], value: int, timeout: float = 10) -> bool:
    """Whether check(value) turns false within timeout seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not check(value):
            return True
        time.sleep(0.1)
    return False


def test_closing_the_terminal_stops_the_engine_and_the_ui(tmp_path: Path) -> None:
    """SIGHUP with the terminal gone (writes fail): run.sh still stops the engine and
    the UI's whole process group, under bash-as-sh and dash."""
    if ui_port_busy():
        pytest.skip("port 1420 is in use (a Newton UI dev server is running)")
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy(RUN, clone / "scripts" / "run.sh")
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "python").symlink_to(sys.executable)
    script(clone / "apps" / "desktop" / "node_modules" / ".bin" / "vite", FAKE_VITE)
    bin_dir, env = mac(tmp_path)
    script(bin_dir / "uv", FAKE_AGENTD)
    for shell in SHELLS:
        port = free_port()
        data = tmp_path / f"data-{shell}"
        vite_pids = tmp_path / f"vite-{shell}.pids"
        shell_env = {
            **env,
            "NEWTON_DATA_DIR": str(data),
            "NEWTON_PORT": str(port),
            "FAKE_VITE_PIDS": str(vite_pids),
        }
        main, tty = os.openpty()
        proc = subprocess.Popen(
            [shell, "scripts/run.sh", "--browser", "--no-open"],
            cwd=clone,
            env=shell_env,
            stdin=tty,
            stdout=tty,
            stderr=tty,
            start_new_session=True,
        )
        os.close(tty)
        output = b""
        try:
            deadline = time.monotonic() + 60
            while b"Newton is at" not in output and time.monotonic() < deadline:
                try:
                    output += os.read(main, 4096)
                except OSError:
                    break
            assert b"Newton is at" in output, output.decode(errors="replace")
            vite, child = (int(pid) for pid in vite_pids.read_text().split())
            assert listening(port) and alive(vite) and alive(child)
            # Once run.sh waits on the UI, closing the terminal sends it SIGHUP twice.
            time.sleep(1)
            os.close(main)  # the terminal window is gone: writes now fail
            main = -1
            proc.send_signal(signal.SIGHUP)
            proc.wait(timeout=60)
            assert stops(listening, port), f"{shell}: engine left running"
            assert stops(alive, vite), f"{shell}: UI left running"
            assert stops(alive, child), f"{shell}: UI group left running"
            assert not (data / "run.pid").exists()
            assert not (data / "run-agentd.pid").exists()
        finally:
            if main >= 0:
                os.close(main)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            # Whatever a failing run.sh left behind.
            pids = [data / "fake-agentd.pid", vite_pids]
            for pid in (int(p) for f in pids if f.exists() for p in f.read_text().split()):
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)


# --- setup.sh: a Python the lock has builds for, and the window's crates -----------


def test_setup_pins_a_python_that_uv_lock_has_mlx_builds_for(tmp_path: Path) -> None:
    """Unpinned, uv takes the newest Python (3.15), for which the locked MLX has no wheel
    and no sdist: `uv sync` would fail on every Apple Silicon Mac."""
    _, env = mac(tmp_path)
    result = run(["sh", str(SETUP), "--dry-run"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(
        r"sync --frozen --python (\d+)\.(\d+) --all-packages --extra mac", result.stdout
    )
    assert match, result.stdout
    tag = f"cp{match[1]}{match[2]}"
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    for name in ("mlx", "pydantic-core"):
        (package,) = (p for p in lock["package"] if p["name"] == name)
        wheels = [w["url"].rsplit("/", 1)[-1] for w in package["wheels"]]
        assert any(f"-{tag}-" in w and "macosx" in w and "arm64" in w for w in wheels), name

    (tmp_path / "intel").mkdir()
    bin_dir, env = mac(tmp_path / "intel", arch="x86_64")
    stub(bin_dir, "sysctl", "echo 0")
    intel = run(["sh", str(SETUP), "--dry-run"], env)
    assert f"sync --frozen --python {match[1]}.{match[2]}\n" in intel.stdout, intel.stdout


def window_mac(tmp_path: Path, cargo_fetch: str) -> tuple[Path, dict[str, str]]:
    """A Mac with Node, pnpm, Rust and the Xcode tools; `cargo fetch` runs cargo_fetch."""
    bin_dir, env = mac(tmp_path)
    stub(bin_dir, "node", "echo v22.12.0")
    stub(bin_dir, "pnpm", f'echo "$*" >>"{tmp_path}/pnpm.calls"; echo 10.33.0')
    stub(bin_dir, "xcode-select", "echo /Library/Developer/CommandLineTools")
    stub(
        bin_dir,
        "cargo",
        f'echo "$*" >>"{tmp_path}/cargo.calls"\n'
        f'case "$1" in fetch) {cargo_fetch} ;; *) echo "cargo 1.90.0" ;; esac',
    )
    return bin_dir, env


def test_setup_downloads_the_windows_rust_packages(tmp_path: Path) -> None:
    """Else the window's first build, on the first (maybe offline) scripts/run.sh,
    has to download every crate."""
    _, env = window_mac(tmp_path, "exit 0")
    result = run(["sh", str(SETUP), "--dry-run"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fetch --manifest-path apps/desktop/src-tauri/Cargo.toml" in result.stdout
    assert "Desktop window (Rust):              yes" in result.stdout


def test_setup_says_when_it_cant_download_the_windows_rust_packages(tmp_path: Path) -> None:
    _, env = window_mac(tmp_path, "echo 'Could not resolve host: index.crates.io' >&2; exit 101")
    # Not a dry run: every program it calls (uv, pnpm, cargo) is a stub.
    result = run(["sh", str(SETUP)], env)
    assert "fetch --manifest-path" in (tmp_path / "cargo.calls").read_text()
    assert "Couldn't download the desktop window's Rust packages" in result.stdout
    assert "Desktop window (Rust):              no" in result.stdout
    assert "Newton in the browser:              yes" in result.stdout
    assert "it opens in your browser" in result.stdout


# --- run.sh: proxies, and a window that can't be built offline -------------------


def test_run_reaches_its_engine_past_an_http_proxy(tmp_path: Path) -> None:
    """curl sends even 127.0.0.1 through http_proxy/all_proxy: the engine would never
    count as Newton's (the first start then times out and stops it)."""
    _, env = mac(tmp_path)
    port = free_port()
    data = tmp_path / "data"
    data.mkdir()
    agentd = tmp_path / "agentd.py"
    agentd.write_text(FAKE_AGENTD)
    server = subprocess.Popen(
        [sys.executable, str(agentd), "--port", str(port), "--data-dir", str(data)]
    )
    try:
        deadline = time.monotonic() + 10
        while not listening(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        dead_proxy = f"http://127.0.0.1:{free_port()}"
        proxied = {
            **env,
            "NEWTON_DATA_DIR": str(data),
            "NEWTON_PORT": str(port),
            "http_proxy": dead_proxy,
            "HTTP_PROXY": dead_proxy,
            "all_proxy": dead_proxy,
            "ALL_PROXY": dead_proxy,
        }
        result = run(["sh", str(RUN), "--status"], proxied)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Engine: running on http://127.0.0.1:{port} " in result.stdout, result.stdout
    finally:
        server.kill()
        server.wait()


def clone_for_run(tmp_path: Path) -> Path:
    """A clone with run.sh, a venv python and a fake vite."""
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy(RUN, clone / "scripts" / "run.sh")
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "python").symlink_to(sys.executable)
    script(clone / "apps" / "desktop" / "node_modules" / ".bin" / "vite", FAKE_VITE)
    return clone


def test_run_opens_the_browser_when_the_window_cant_be_built_offline(tmp_path: Path) -> None:
    """The window's crates aren't downloaded and this Mac is offline: tauri dev would
    fail and stop the engine, so run.sh opens the browser UI instead."""
    if ui_port_busy():
        pytest.skip("port 1420 is in use (a Newton UI dev server is running)")
    clone = clone_for_run(tmp_path)
    bin_dir, env = window_mac(
        tmp_path, "echo 'Could not resolve host: index.crates.io' >&2; exit 101"
    )
    script(bin_dir / "uv", FAKE_AGENTD)
    port = free_port()
    data = tmp_path / "data"
    vite_pids = tmp_path / "vite.pids"
    env = {
        **env,
        "NEWTON_DATA_DIR": str(data),
        "NEWTON_PORT": str(port),
        "FAKE_VITE_PIDS": str(vite_pids),
    }
    proc = subprocess.Popen(
        ["sh", "scripts/run.sh", "--no-open"],
        cwd=clone,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = b""
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + 60
        while b"Newton is at" not in output and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            output += line
        text = output.decode(errors="replace")
        assert "Newton opens in the browser instead" in text, text
        assert "Newton is at" in text, text
        assert listening(port), "the engine was stopped"
        assert not (tmp_path / "pnpm.calls").exists(), "tauri dev was started"
        calls = (tmp_path / "cargo.calls").read_text()
        assert "fetch --offline --manifest-path apps/desktop/src-tauri/Cargo.toml" in calls
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
        assert stops(listening, port), "engine left running"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        pids = [data / "fake-agentd.pid", vite_pids]
        for pid in (int(p) for f in pids if f.exists() for p in f.read_text().split()):
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_run_points_to_the_browser_when_the_window_fails(tmp_path: Path) -> None:
    if ui_port_busy():
        pytest.skip("port 1420 is in use (a Newton UI dev server is running)")
    clone = clone_for_run(tmp_path)
    bin_dir, env = window_mac(tmp_path, "exit 0")
    stub(bin_dir, "pnpm", f'echo "$*" >>"{tmp_path}/pnpm.calls"; exit 1')
    script(bin_dir / "uv", FAKE_AGENTD)
    port = free_port()
    data = tmp_path / "data"
    env = {**env, "NEWTON_DATA_DIR": str(data), "NEWTON_PORT": str(port)}
    try:
        result = run(["sh", "scripts/run.sh"], env, cwd=clone)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "--dir apps/desktop tauri dev" in (tmp_path / "pnpm.calls").read_text()
        assert "Newton also runs in your browser: scripts/run.sh --browser" in result.stderr
        assert stops(listening, port), "engine left running"
    finally:
        pid_file = data / "fake-agentd.pid"
        if pid_file.exists() and alive(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
