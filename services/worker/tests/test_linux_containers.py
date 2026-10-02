"""bootstrap.sh on real Ubuntu images (opt-in: NEWTON_TEST_DOCKER=1, needs Docker
and network). The closest thing to a DGX Spark without one: DGX OS is Ubuntu 24.04
on aarch64, with a PEP 668 system Python, dash as /bin/sh and util-linux flock.

Each scenario installs only the listed apt packages, bootstraps as a non-root
user (real PyPI numpy), then restarts with no package index to prove restarts
never need the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWTON_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set NEWTON_TEST_DOCKER=1 (needs Docker + network)",
)

SCRIPT = r"""
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends python3 {packages} ca-certificates >/dev/null
useradd -m -s /bin/sh spark
install -d -o spark -m 700 /home/spark/.newton
cp -r /src /home/spark/.newton/src
printf t0ken > /home/spark/.newton/token
chown -R spark /home/spark/.newton
run() {{
  su spark -c "cd ~ && $1 sh ~/.newton/src/bootstrap.sh --python python3" >/tmp/out 2>/tmp/err \
    && rc=0 || rc=$?
  echo "RC=$rc"
  echo "JSON=$(tail -n 1 /tmp/out)"
  echo "ERR=$(tail -n 3 /tmp/err | tr '\n' ' ')"
}}
run ""
run "PIP_NO_INDEX=1 UV_OFFLINE=1"
echo "FLOCK=$(command -v flock || echo none)"
echo "SOCKET=$(test -S /home/spark/.newton/worker.sock && echo yes || echo no)"
"""


def bootstrap_in(image: str, packages: str) -> list[dict[str, str]]:
    script = SCRIPT.format(packages=packages)
    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{WORKER_DIR}:/src:ro", image, "bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    runs: list[dict[str, str]] = []
    extra: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "RC":
            runs.append({"rc": value})
        elif key in ("JSON", "ERR") and runs:
            runs[-1][key.lower()] = value
        elif key in ("FLOCK", "SOCKET"):
            extra[key.lower()] = value
    for run in runs:
        run.update(extra)
    return runs


def status(run: dict[str, str]) -> dict[str, object]:
    assert run["rc"] == "0", run
    data: dict[str, object] = json.loads(run["json"])
    return data


def test_dgx_like_ubuntu_2404_without_python3_venv() -> None:
    first, restart = bootstrap_in("ubuntu:24.04", "python3-pip")
    st = status(first)
    assert st["running"] is True
    assert str((st["deps"] or {})["numpy"]).startswith("2.")  # type: ignore[index]
    assert st["bootstrap"]["installer"] == "system-pip"  # type: ignore[index]
    assert first["flock"] != "none" and first["socket"] == "yes"
    assert status(restart)["bootstrap"]["installer"] == "satisfied"  # type: ignore[index]


def test_bare_ubuntu_2404_says_what_to_install() -> None:
    first, _ = bootstrap_in("ubuntu:24.04", "")
    assert first["rc"] == "69", first
    assert "sudo apt install python3-venv python3-pip" in first["err"]


def test_ubuntu_2204_with_old_pip() -> None:
    first, restart = bootstrap_in("ubuntu:22.04", "python3-pip")
    st = status(first)
    assert st["bootstrap"]["installer"] == "system-pip-target"  # type: ignore[index]
    assert str((st["deps"] or {})["numpy"]).startswith("2.")  # type: ignore[index]
    assert status(restart)["bootstrap"]["installer"] == "satisfied"  # type: ignore[index]


def test_ubuntu_2404_with_python3_venv() -> None:
    first, restart = bootstrap_in("ubuntu:24.04", "python3-venv")
    assert status(first)["bootstrap"]["installer"] == "venv-pip"  # type: ignore[index]
    assert status(restart)["bootstrap"]["installer"] == "satisfied"  # type: ignore[index]


GPU_SCRIPT = r"""
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends python3 python3-pip ca-certificates >/dev/null
useradd -m -s /bin/sh spark
install -d -o spark -m 700 /home/spark/.newton
cp -r /src /home/spark/.newton/src
printf t0ken > /home/spark/.newton/token
chown -R spark /home/spark/.newton
run() {{  # $1 = env assignments, $2 = extra bootstrap.sh flags
  su spark -c "cd ~ && $1 sh ~/.newton/src/bootstrap.sh --python python3 ${{2:-}}" \
    >/tmp/out 2>/tmp/err && rc=0 || rc=$?
  echo "RC=$rc"
  echo "JSON=$(tail -n 1 /tmp/out)"
  echo "ERR=$(tail -n 3 /tmp/err | tr '\n' ' ')"
  echo "GPU=$(tr -d '\n' </home/spark/.newton/gpu.json 2>/dev/null || echo null)"
}}
{steps}
"""


def gpu_bootstrap_in(steps: str, tmp_path: Path) -> list[dict[str, str]]:
    from pyenv_shims import build_wheelhouse, make_fake_nvidia_smi

    wheels = build_wheelhouse(tmp_path / "wheels")
    make_fake_nvidia_smi(tmp_path / "fakebin")
    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{WORKER_DIR}:/src:ro", "-v", f"{wheels}:/wheels:ro",
         "-v", f"{tmp_path / 'fakebin'}:/fakebin:ro", "ubuntu:24.04", "bash", "-c",
         GPU_SCRIPT.format(steps=steps)],
        capture_output=True,
        text=True,
        timeout=1800,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-2000:]
    runs: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "RC":
            runs.append({"rc": value})
        elif key in ("JSON", "ERR", "GPU") and runs:
            runs[-1][key.lower()] = value
    return runs


INSTALL_DRIVER = "cp /fakebin/nvidia-smi /usr/local/bin/nvidia-smi"


def test_dgx_like_ubuntu_gets_gpu_support_from_system_pip(tmp_path: Path) -> None:
    # Real numpy from PyPI first (no GPU yet), then a driver appears and CuPy comes
    # from the local wheelhouse: the fake GPU passes, then needs the PyPI toolkit.
    cpu, gpu, fallback = gpu_bootstrap_in(
        f"""run ""
{INSTALL_DRIVER}
run "PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels" "--gpu-only auto"
su spark -c 'python3 -m pip --python ~/.newton/venv/bin/python uninstall -y -q cupy-cuda13x' \
  >/dev/null 2>&1 || true
echo nvrtc > /home/spark/fake-cupy && chown spark /home/spark/fake-cupy
rm /home/spark/.newton/gpu.json
run "PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels" "--gpu-only auto"
""",
        tmp_path,
    )
    assert status(cpu)["running"] and cpu["gpu"] == "null"
    assert status(cpu)["bootstrap"]["installer"] == "system-pip"  # type: ignore[index]
    for run in (gpu, fallback):
        assert run["rc"] == "0" and json.loads(run["json"]) == json.loads(run["gpu"]), run
    state = json.loads(gpu["gpu"])
    assert state["ok"] is True and state["dist"] == "cupy-cuda13x", state
    assert state["unified_memory"] is True  # the fake GB10 reports memory [N/A]
    healed = json.loads(fallback["gpu"])
    assert healed["ok"] is True and healed["fallback_tried"] is True, healed


def test_real_cupy_without_a_gpu_is_recorded_not_fatal(tmp_path: Path) -> None:
    # The real CuPy wheel from PyPI in a container with no GPU (only a fake
    # nvidia-smi): the real failure message must be recorded, must not trigger the
    # multi-GB toolkit download (the driver library is what's missing), and the
    # host must keep serving.
    cpu, run = gpu_bootstrap_in(f'{INSTALL_DRIVER}\nrun ""\nrun "" "--gpu-only auto"', tmp_path)
    assert status(cpu)["running"] is True and run["rc"] == "0", run
    state = json.loads(run["gpu"])
    assert state["ok"] is False and state["dist"] == "cupy-cuda13x", state
    assert str(state["cupy"]).startswith("14."), state
    assert "fallback_tried" not in state, state
    print("real CuPy failure:", state["error"])
