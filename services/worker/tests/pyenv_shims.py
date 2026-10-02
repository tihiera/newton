"""Hermetic stand-ins for the Python environments found on real hosts.

- `build_wheelhouse`: tiny fake `numpy`/`matplotlib` wheels, so bootstrap's real
  pip/uv installs run offline (point PIP_FIND_LINKS / UV_FIND_LINKS at it).
- `make_python_shim`: a `python3` that behaves like a distro Python: missing
  ensurepip (Ubuntu without python3-venv), PEP 668 externally managed, no pip,
  old pip without `--python`, or no venv module at all. Everything else is
  delegated to a real interpreter that has pip.

Also used by services/agentd/tests (imported through sys.path).
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

FAKE_NUMPY = "2.1.0"  # what `numpy>=2,<2.6` resolves to in the fake wheelhouse
FAKE_MATPLOTLIB = "3.9.0"
OLD_NUMPY = "1.26.4"  # what Ubuntu 24.04's apt numpy is; must get upgraded
TOO_NEW_NUMPY = "2.6.0"  # outside the CuPy 14 range; must never be picked
FAKE_CUPY = "14.2.0"
OLD_CUPY = "14.0.1"
TOOLKIT_EXTRAS = ("cublas", "cudart", "cufft", "curand", "cusolver", "cusparse", "nvrtc")
FAKE_DRIVER = "580.95.05"

# The fake GPU. Its behaviour comes from $HOME/fake-cupy (HOME is one of the few
# variables the smoke test passes on): ok | nvrtc (CUDA libraries missing until the
# cuda-toolkit wheels are installed) | sigbus | hang | wrong | import-error.
FAKE_CUPY_SOURCE = """
import os, signal, time
from importlib import metadata


def _has_toolkit():
    try:
        metadata.version("cuda-toolkit")
    except metadata.PackageNotFoundError:
        return False
    return True


__version__ = "@VERSION@"
try:
    with open(os.path.join(os.environ.get("HOME", "/nonexistent"), "fake-cupy")) as f:
        _MODE = f.read().strip()
except OSError:
    _MODE = "ok"
if _MODE == "import-error":
    raise ImportError("libcuda.so.1: cannot open shared object file")
if _MODE == "banner" and not _has_toolkit():
    # How CuPy 14 re-raises a failed import: the cause sits inside a banner.
    raise ImportError(
        "\\n================================================================\\n"
        "Failed to import CuPy.\\n\\nOriginal error:\\n"
        "  ImportError: libcublas.so.13: cannot open shared object file\\n"
        "================================================================\\n"
    )
float64, float32 = "float64", "float32"


class _Arr:
    def __init__(self, values):
        self.v = list(values)
    def astype(self, dtype):
        return _Arr(self.v)
    def sum(self):
        return _Arr([sum(self.v)])
    def __float__(self):
        return float(self.v[0])
    def __getitem__(self, s):
        return _Arr(self.v[s])
    def get(self):
        return self
    def tolist(self):
        return list(self.v)
    def __gt__(self, other):
        return _Arr(x > y for x, y in zip(self.v, other.v))


def arange(n, dtype=None):
    return _Arr(float(i) for i in range(n))
def where(c, a, b):
    return _Arr(x if t else y for t, x, y in zip(c.v, a.v, b.v))
def roll(a, k):
    return _Arr(a.v[-k:] + a.v[:-k])
def abs(a):
    return _Arr(x if x >= 0 else -x for x in a.v)
def maximum(a, s):
    return _Arr(x if x > s else s for x in a.v)


class DynamicLibNotFoundError(RuntimeError):
    pass


class ElementwiseKernel:
    def __init__(self, *args):
        if _MODE == "pathfinder" and not _has_toolkit():
            # cuda-pathfinder: the cause first, then listings of every dir it searched.
            raise DynamicLibNotFoundError(
                'Failure finding "libnvrtc.so.13": not found in site-packages\\n'
                '  listdir("/usr/local/cuda-11.8/lib64"):\\n    libnvrtc.so.11.2\\n    stubs'
            )
        if _MODE == "nvrtc" and not _has_toolkit():
            raise RuntimeError("CompileException: libnvrtc.so.13: cannot open shared object file")
        if _MODE == "sigbus":
            os.kill(os.getpid(), signal.SIGBUS)
        if _MODE == "hang":
            time.sleep(3600)
    def __call__(self, a):
        if _MODE == "wrong":
            return _Arr(2.0 * x for x in a.v)
        return _Arr(2.0 * x + 1.0 for x in a.v)


class _Runtime:
    def getDeviceCount(self):
        return 1
    def getDeviceProperties(self, i):
        return {"name": b"NVIDIA GB10", "major": 12, "minor": 1}
    def runtimeGetVersion(self):
        return 13000
    def driverGetVersion(self):
        return 13000
    def memGetInfo(self):
        return (1 << 30, 1 << 37)


class _Device:
    def synchronize(self):
        pass


class _Pool:
    def free_all_blocks(self):
        pass


class cuda:
    runtime = _Runtime()
    Device = _Device


def get_default_memory_pool():
    return _Pool()
""".replace("@VERSION@", FAKE_CUPY)

# Tools bootstrap.sh, the upload command and the worker's `stop` use. Tests get a
# PATH with exactly these (symlinked), so a uv or python on the developer's or
# CI image's PATH can't leak into the simulated host.
HOST_TOOLS = (
    "sh", "dash", "bash", "cat", "chmod", "dirname", "find", "grep", "head", "ls", "mkdir", "mv",
    "nohup", "ps", "rm", "sed", "setsid", "sleep", "tail", "tar", "uname", "env", "csh",
)  # fmt: skip


def _record_hash(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(
    name: str,
    version: str,
    out_dir: Path,
    requires: tuple[str, ...] = (),
    module: str | None = None,
    source: str | None = None,
    extras: tuple[str, ...] = (),
) -> Path:
    normalized = name.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    requires_dist = "".join(f"Requires-Dist: {r}\n" for r in requires)
    provides = "".join(f"Provides-Extra: {e}\n" for e in extras)
    module = module or normalized
    files = {
        f"{module}/__init__.py": (source or f'__version__ = "{version}"\n').encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{provides}{requires_dist}"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: newton-tests\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = [f"{path},sha256={_record_hash(data)},{len(data)}" for path, data in files.items()]
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for name_in_zip, data in files.items():
            zf.writestr(name_in_zip, data)
    return path


def build_wheelhouse(out_dir: Path) -> Path:
    for version in (OLD_NUMPY, FAKE_NUMPY, TOO_NEW_NUMPY):
        build_wheel("numpy", version, out_dir)
    # Like the real one: an unpinned matplotlib install would pull the newest numpy.
    build_wheel("matplotlib", FAKE_MATPLOTLIB, out_dir, requires=("numpy>=1.23",))
    # GPU support: CuPy as published (numpy pin + the `ctk` extra pulling NVIDIA's
    # cuda-toolkit wheels), with a pure-Python stand-in for the GPU.
    for dist, toolkit in (("cupy-cuda13x", "13"), ("cupy-cuda12x", "12")):
        build_wheel(
            dist, FAKE_CUPY, out_dir, module="cupy", source=FAKE_CUPY_SOURCE, extras=("ctk",),
            requires=("numpy<2.6,>=2.0",
                      f'cuda-toolkit[cublas,cudart,nvrtc]=={toolkit}.*; extra == "ctk"'),
        )  # fmt: skip
    # An older CuPy, to test upgrades (stale metadata must go).
    build_wheel(
        "cupy-cuda13x", OLD_CUPY, out_dir, module="cupy",
        source=FAKE_CUPY_SOURCE.replace(FAKE_CUPY, OLD_CUPY), extras=("ctk",),
        requires=("numpy<2.6,>=2.0", 'cuda-toolkit[cublas,cudart,nvrtc]==13.*; extra == "ctk"'),
    )  # fmt: skip
    # Unlike CuPy, the toolkit wheels leave numpy unbounded: only bootstrap's own
    # pin keeps the resolver off numpy 2.6 when they are installed.
    for version in ("12.9.1", "13.0.2", "13.4.2"):
        build_wheel("cuda-toolkit", version, out_dir, module="cuda_toolkit_fake",
                    extras=TOOLKIT_EXTRAS, requires=("numpy>=2",))  # fmt: skip
    return out_dir


def make_fake_nvidia_smi(bindir: Path, driver: str = FAKE_DRIVER, fail: str | None = None) -> Path:
    """nvidia-smi as on a GB10 (memory [N/A]); `fail` makes every query fail like
    a driver/library mismatch does."""
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / "nvidia-smi"
    if fail:
        body = f'echo "{fail}"\nexit 9\n'
    else:
        body = (
            'case "$*" in\n'
            f'  *query-gpu=driver_version*) echo "{driver}" ;;\n'
            '  *query-gpu=index,compute_cap*) echo "0, 12.1" ;;\n'
            f'  *query-gpu=*) echo "0, NVIDIA GB10, [N/A], [N/A], {driver}, 0" ;;\n'
            "  *) echo 'NVIDIA-SMI' ;;\n"
            "esac\n"
        )
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def build_tool_path(bindir: Path) -> str:
    bindir.mkdir(parents=True, exist_ok=True)
    for tool in HOST_TOOLS:
        src = shutil.which(tool, path="/usr/bin:/bin:/usr/sbin:/sbin")
        if src and not (bindir / tool).exists():
            (bindir / tool).symlink_to(src)
    assert shutil.which("uv", path=str(bindir)) is None
    return str(bindir)


def pip_install_into(base_python: Path, venv_python: Path, wheelhouse: Path, *reqs: str) -> None:
    """Seed a venv (with or without its own pip) from the fake wheelhouse."""
    subprocess.run(
        [str(base_python), "-m", "pip", "--python", str(venv_python), "install", "--quiet",
         "--no-index", "--find-links", str(wheelhouse), *reqs],
        check=True,
        capture_output=True,
    )  # fmt: skip


def make_base_python(dest: Path) -> Path:
    """A real interpreter with pip (a venv of the test interpreter)."""
    subprocess.run([sys.executable, "-m", "venv", str(dest)], check=True, capture_output=True)
    return dest / "bin" / "python"


def make_python_shim(
    path: Path,
    base_python: Path,
    *,
    no_ensurepip: bool = False,
    no_venv: bool = False,
    pep668: bool = False,
    no_pip: bool = False,
    old_pip: bool = False,
) -> Path:
    rules = []
    if no_venv:
        rules.append(
            'if [ "$1" = -m ] && [ "$2" = venv ]; then '
            'echo "/usr/bin/python3: No module named venv" >&2; exit 1; fi'
        )
    if no_ensurepip:
        rules.append(
            'if [ "$1" = -m ] && [ "$2" = venv ]; then case " $* " in *" --without-pip "*) ;; '
            '*) echo "The virtual environment was not created successfully because ensurepip '
            'is not available." >&2; exit 1 ;; esac; fi'
        )
    if no_pip:
        rules.append(
            'if [ "$1" = -m ] && [ "$2" = pip ]; then '
            'echo "/usr/bin/python3: No module named pip" >&2; exit 1; fi'
        )
    if old_pip:
        rules.append(
            'if [ "$1" = -m ] && [ "$2" = pip ] && [ "$3" = --version ]; then '
            'echo "pip 22.0.2 from /usr/lib/python3/dist-packages/pip (python 3.10)"; exit 0; fi'
        )
        rules.append(
            'if [ "$1" = -m ] && [ "$2" = pip ] && [ "$3" = --python ]; then '
            'echo "no such option: --python" >&2; exit 2; fi'
        )
    if pep668:
        rules.append(
            'if [ "$1" = -m ] && [ "$2" = pip ] && [ "$3" = install ]; then '
            'case " $* " in *" --target "*) ;; *) echo "error: externally-managed-environment" '
            ">&2; exit 1 ;; esac; fi"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + "\n".join(rules) + f'\nexec "{base_python}" "$@"\n')
    path.chmod(0o755)
    return path


def offline_index_env(wheelhouse: Path, cache_dir: Path) -> dict[str, str]:
    """Environment that makes pip and uv install only from the fake wheelhouse."""
    return {
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_NO_INDEX": "1",
        "UV_FIND_LINKS": str(wheelhouse),
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(cache_dir),
        "UV_PYTHON_DOWNLOADS": "never",
    }


@pytest.fixture(scope="session")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_wheelhouse(tmp_path_factory.mktemp("wheelhouse"))


@pytest.fixture(scope="session")
def empty_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("empty-wheelhouse")


@pytest.fixture(scope="session")
def tool_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    return build_tool_path(tmp_path_factory.mktemp("host-tools"))


@pytest.fixture(scope="session")
def base_python(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    yield make_base_python(tmp_path_factory.mktemp("base-python") / "venv")


def find_dash() -> str:
    """Ubuntu's /bin/sh is dash; test the bootstrap with it when available."""
    for candidate in ("/bin/dash", "/usr/bin/dash"):
        if os.path.exists(candidate):
            return candidate
    return "/bin/sh"
