"""SV4: model services on this Mac with MLX. Small 4-bit mlx-community models at a
pinned commit, fixed argv only, and a gate: AC power, no memory pressure."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from newton_worker import services as svc
from newton_worker.jobs import JobError
from test_services import fake, store, wait_state  # noqa: F401  (fixture)

REV = "a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3"
MLX = {"service_id": "m", "engine": "mlx", "model": "mlx-community/Qwen2.5-0.5B-Instruct-4bit",
       "revision": REV, "memory_gb": 1}  # fmt: skip


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"model": "Qwen/Qwen2.5-0.5B-Instruct"}, "invalid model name"),  # not mlx-community
        ({"model": "mlx-community/Llama-3-8B-Instruct-8bit"}, "invalid model name"),  # not 4-bit
        ({"model": "mlx-community/../../etc-4bit"}, "invalid model name"),
        ({"revision": "main"}, "pinned revision"),
        ({"revision": None}, "pinned revision"),
        ({"memory_gb": 32}, "small"),
    ],
)
def test_only_small_pinned_mlx_community_models(change: dict[str, Any], message: str) -> None:
    with pytest.raises(JobError) as e:
        svc.validate_spec({**MLX, **change})
    assert message in e.value.message


def test_mlx_runs_a_fixed_argv_on_the_exact_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "engine_executable", lambda engine: "/py")
    spec = svc.validate_spec(MLX)
    argv, env = svc.engine_command(spec, 4321, tmp_path)
    snapshot = tmp_path / "models/mlx/models--mlx-community--Qwen2.5-0.5B-Instruct-4bit/snapshots"
    assert argv[:4] == ["/py", "-m", "mlx_lm", "server"]
    assert argv[argv.index("--model") + 1] == str(snapshot / REV)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--allowed-origins") + 1] == "http://127.0.0.1:1"  # no browser
    assert env["HF_HUB_OFFLINE"] == "1"  # the engine never downloads anything itself
    assert env["MLX_MPI_LIBNAME"] == "libnewton-no-mpi.dylib"  # never MPI
    fetch = svc.fetch_command(spec, tmp_path)
    assert fetch == ["/py", "-m", "huggingface_hub.cli.hf", "download", MLX["model"],
                     "--revision", REV, "--cache-dir", str(tmp_path / "models/mlx"),
                     *[a for f in svc.MLX_FILES for a in ("--include", f)],
                     "--quiet"]  # fmt: skip
    assert not svc.model_present(spec, tmp_path)
    (snapshot / REV).mkdir(parents=True)
    (snapshot / REV / "config.json").write_text("{}")
    assert not svc.model_present(spec, tmp_path)  # a partial download isn't the model
    (snapshot / REV / svc.MLX_COMPLETE).write_text("{}")
    assert svc.model_present(spec, tmp_path)
    assert argv[argv.index("--prompt-cache-bytes") + 1] == str(int(1 * svc.GIB / 4))
    assert argv[argv.index("--kv-bits") + 1] == "8"  # q8_0, the default


def test_the_gate_can_only_be_closed_from_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, power: Path
) -> None:
    gate_file = tmp_path / "gate"
    monkeypatch.setenv("NEWTON_MAC_GATE_FILE", str(gate_file))
    gate_file.write_text("battery")
    assert svc.mac_gate()["ok"] is False and "battery" in svc.mac_gate()["reason"]
    gate_file.write_text("pressure")
    assert "memory pressure" in svc.mac_gate()["reason"]
    gate_file.write_text("ok, honestly")  # not a closing word: the real state (on AC) holds
    assert svc.mac_gate()["ok"] is True


def test_admission_refuses_a_mac_model_on_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEWTON_MAC_GATE", "battery")
    monkeypatch.setattr(svc, "memory_now", lambda: {"total": 64 * svc.GIB,
                                                    "available": 40 * svc.GIB})  # fmt: skip
    out = svc.ServiceStore(tmp_path).admission(svc.validate_spec(MLX))
    assert out["ok"] is False and "battery" in out["sizing"]
    assert out["mac_gate"]["ac_power"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="the gate reads macOS power state")
def test_a_running_mac_model_stops_when_the_mac_goes_on_battery(
    store: svc.ServiceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    power: Path,  # starts on (faked) AC power, whatever this Mac is on
) -> None:
    gate_file = tmp_path / "gate"
    monkeypatch.setenv("NEWTON_MAC_GATE_FILE", str(gate_file))
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    store.create(fake("gated", mac_gated=True))
    wait_state(store, "gated", ("ready",))
    gate_file.write_text("battery")
    stopped = wait_state(store, "gated", ("stopped",), timeout=30)
    assert "on battery" in (stopped["error"] or "")


# -- review round: no dependence on this Mac's power state; the download phase ------------


@pytest.fixture
def power(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake pmset and sysctl first on PATH (the supervisor inherits PATH): on AC power,
    pressure level 1, until a test says otherwise through the two files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "power"
    state.mkdir()
    (state / "battery").write_text("Now drawing from 'AC Power'\n")
    (state / "level").write_text("1\n")
    for name, body in (("pmset", f"cat {state / 'battery'}"),
                       ("sysctl", f"cat {state / 'level'}")):  # fmt: skip
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.delenv("NEWTON_MAC_GATE", raising=False)
    monkeypatch.delenv("NEWTON_MAC_GATE_FILE", raising=False)
    return state


@pytest.mark.parametrize(
    ("battery", "level", "ok", "ac", "pressure"),
    [
        ("Now drawing from 'AC Power'\n", "1", True, True, 1),
        ("Now drawing from 'Battery Power'\n -InternalBattery-0 80%\n", "1", False, False, 1),
        ("Now drawing from 'AC Power'\n", "2", True, True, 2),  # warning: busy, not a stop
        ("Now drawing from 'AC Power'\n", "4", False, True, 4),
        ("", "", True, None, None),  # can't tell: not a reason to stop
        ("garbage", "x", True, None, None),  # unreadable: can't tell either
    ],
)
def test_the_gate_reads_power_and_pressure(power: Path, battery: str, level: str, ok: bool,
                                           ac: Any, pressure: Any) -> None:  # fmt: skip
    (power / "battery").write_text(battery)
    (power / "level").write_text(level)
    gate = svc.mac_gate()
    assert (gate["ok"], gate["ac_power"], gate["memory_pressure"]) == (ok, ac, pressure)


def test_the_gate_file_cannot_open_a_closed_gate(
    power: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (power / "battery").write_text("Now drawing from 'Battery Power'\n")
    gate_file = tmp_path / "gate"
    gate_file.write_text("ok")
    monkeypatch.setenv("NEWTON_MAC_GATE_FILE", str(gate_file))
    assert svc.mac_gate()["ok"] is False


def supervisor_for(tmp_path: Path, fetch_code: str, memory_gb: float = 1) -> Any:
    from newton_worker.fsutil import write_json_atomic
    from newton_worker.run_service import Supervisor

    d = tmp_path / "services" / "m"
    d.mkdir(parents=True)
    spec = svc.validate_spec({**MLX, "service_id": "m", "memory_gb": memory_gb})
    snapshot = tmp_path / "snap" / REV
    write_json_atomic(d / "spec.json", spec)
    write_json_atomic(d / "status.json", {"state": "starting", "port": 1})
    write_json_atomic(d / "command.json", {
        "argv": ["python", "--model", str(snapshot)], "env": {},
        "fetch": [sys.executable, "-c", fetch_code.replace("SNAP", str(snapshot))],
    })  # fmt: skip
    s = Supervisor(d)
    s.listed_weights = lambda: None  # type: ignore[method-assign]  # offline
    return s, snapshot


DOWNLOAD = ("import pathlib, sys; p = pathlib.Path(r'SNAP'); p.mkdir(parents=True); "
            "(p / 'config.json').write_text('{}'); "
            "(p / 'model.safetensors').write_bytes(b'x' * SIZE)")  # fmt: skip


def test_a_complete_download_is_marked(tmp_path: Path) -> None:
    s, snapshot = supervisor_for(tmp_path, DOWNLOAD.replace("SIZE", str(1000)))
    s.fetch(time.time() + 30)
    assert (snapshot / svc.MLX_COMPLETE).is_file()
    assert s.status["state"] == "loading" and s.status["fetch_pid"] is None
    s.fetch(time.time() + 30)  # done before: nothing downloaded again


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("import sys; sys.exit(1)", "download failed"),
        ("pass", "was not downloaded"),
        (DOWNLOAD.replace("SIZE", str(30 * 1024 * 1024)), "more than the"),
    ],
)
def test_a_bad_download_fails_and_is_not_marked(tmp_path: Path, code: str, message: str) -> None:
    s, snapshot = supervisor_for(tmp_path, code, memory_gb=0.01)  # ~10 MB declared
    with pytest.raises(RuntimeError, match=message):
        s.fetch(time.time() + 30)
    assert not (snapshot / svc.MLX_COMPLETE).exists()


def test_the_listed_size_refuses_before_downloading(tmp_path: Path) -> None:
    s, snapshot = supervisor_for(tmp_path, DOWNLOAD.replace("SIZE", str(10)))
    s.listed_weights = lambda: 40 * svc.GIB  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="not a small model"):
        s.fetch(time.time() + 30)
    assert not snapshot.exists()  # nothing was downloaded


def test_a_stop_ends_the_download_and_its_group(tmp_path: Path) -> None:
    from newton_worker.jobs import pid_alive
    from newton_worker.run_service import Stop

    s, _ = supervisor_for(tmp_path, "import subprocess, sys, time; "
                          "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                          " time.sleep(60)")  # fmt: skip
    seen: dict[str, Any] = {}
    real_update = s.update

    def update(**fields: Any) -> None:
        real_update(**fields)
        if fields.get("fetch_pid"):
            seen["pid"] = fields["fetch_pid"]
            (s.dir / "stop").touch()  # asked to stop once the download runs

    s.update = update  # type: ignore[method-assign]
    with pytest.raises(Stop):
        s.fetch(time.time() + 30)
    assert seen["pid"] and not pid_alive(seen["pid"])
    assert s.status["fetch_pid"] is None


def test_the_download_has_a_deadline(tmp_path: Path) -> None:
    s, _ = supervisor_for(tmp_path, "import time; time.sleep(60)")
    with pytest.raises(RuntimeError, match="did not finish in time"):
        s.fetch(time.time() + 1)


def test_concurrency_and_kv_bits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "engine_executable", lambda engine: "/py")
    spec = svc.validate_spec({**MLX, "parallel": 8, "kv_cache_type": "f16"})
    argv, _ = svc.engine_command(spec, 1, tmp_path)
    assert argv[argv.index("--decode-concurrency") + 1] == "8"
    assert argv[argv.index("--prompt-concurrency") + 1] == "4"
    assert "--kv-bits" not in argv  # f16: full precision
    argv, _ = svc.engine_command(svc.validate_spec({**MLX, "kv_cache_type": "q4_0"}), 1, tmp_path)
    assert argv[argv.index("--kv-bits") + 1] == "4"


def test_mlx_is_found_only_on_apple_silicon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.sys, "platform", "linux")
    assert svc.engine_executable("mlx") is None
    monkeypatch.setattr(svc.sys, "platform", "darwin")
    monkeypatch.setattr(svc.platform, "machine", lambda: "x86_64")
    assert svc.engine_executable("mlx") is None
    monkeypatch.setattr(svc.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(svc.importlib.util, "find_spec", lambda name: None)
    assert svc.engine_executable("mlx") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="a Mac's gate")
def test_unplugged_while_loading_never_gets_ready(
    store: svc.ServiceStore,
    power: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    monkeypatch.setenv("NEWTON_SERVICE_RESERVE_GB", "0.25")
    store.create(fake("loading", mac_gated=True, startup_delay_s=20))
    wait_state(store, "loading", ("loading", "starting"))
    (power / "battery").write_text("Now drawing from 'Battery Power'\n")
    stopped = wait_state(store, "loading", ("stopped", "ready"), timeout=40)
    assert stopped["state"] == "stopped" and "on battery" in stopped["error"]
    assert stopped["ready_at"] is None
