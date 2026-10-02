"""Model services next to jobs (SV1): long-running inference servers (an OpenAI-
compatible endpoint on 127.0.0.1) started, watched and stopped by the worker.

A service is described by a typed ServiceSpec: an engine from a fixed list, a model
with a pinned revision, and a few typed settings. Nothing in it reaches a shell or
an engine as a free-form flag: each engine's argv is built here from the fields.

Each service runs under a detached supervisor (`python -m newton_worker.run_service
<dir>`), like jobs run under run_job: it survives the worker and the SSH session.
Before anything starts, memory admission checks that the host keeps a reserve
free: on a GB10 the GPU allocates from system RAM, and running out freezes the
whole machine instead of failing one process.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .fsutil import read_json, write_json_atomic
from .jobs import JobError, pid_alive, process_command_line
from .procs import last_argument_is, stop_group

SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
ENGINES = ("ollama", "vllm", "mlx", "fake")
OLLAMA_MODEL_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,99}(/[a-z0-9][a-z0-9._-]{0,99})?(:[A-Za-z0-9._-]{1,64})?$"
)
HF_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
FAKE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}$")
# Mac models (SV4): small 4-bit conversions from mlx-community only.
MLX_MODEL_RE = re.compile(
    r"^mlx-community/[A-Za-z0-9][A-Za-z0-9._-]{0,95}-4bit(-[A-Za-z0-9._-]+)?$"
)
MLX_MAX_GB = 16.0  # "small": a Mac shares this memory with everything else
MLX_COMPLETE = ".newton-complete"
# What mlx-lm loads; nothing else of the repository is downloaded.
MLX_FILES = ("*.json", "*.safetensors", "tokenizer.model", "*.tiktoken", "*.txt", "*.jinja")
OLLAMA_DIGEST_RE = re.compile(r"^[0-9a-f]{12,64}$")  # manifest digest (a prefix is enough)
HF_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")  # a commit: tags and branches move
KV_CACHE_TYPES = ("f16", "q8_0", "q4_0")
ACTIVE = ("starting", "loading", "ready", "draining", "stopping")
TERMINAL = ("stopped", "failed", "lost")
API_KEY_ENV = {"vllm": "VLLM_API_KEY", "fake": "NEWTON_FAKE_API_KEY"}  # ollama has none
API_KEY_RE = re.compile(r"[A-Za-z0-9_-]{20,200}")
GIB = 1024**3


def validate_spec(spec: Any) -> dict[str, Any]:
    """The worker's own check of a ServiceSpec (agentd validates it too)."""
    if not isinstance(spec, dict):
        raise JobError(400, "service spec must be an object")
    allowed = {
        "service_id", "engine", "model", "revision", "context_length", "parallel",
        "kv_cache_type", "memory_gb", "startup_timeout_s", "trust_remote_code", "fake",
    }  # fmt: skip
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise JobError(400, f"unknown service fields: {', '.join(unknown)}")
    sid = spec.get("service_id")
    if not isinstance(sid, str) or not SERVICE_ID_RE.fullmatch(sid):
        raise JobError(400, "invalid service_id")
    engine = spec.get("engine")
    if engine not in ENGINES:
        raise JobError(400, f"engine must be one of {list(ENGINES)}")
    model, revision = spec.get("model"), spec.get("revision")
    pattern = {"ollama": OLLAMA_MODEL_RE, "vllm": HF_MODEL_RE, "mlx": MLX_MODEL_RE,
               "fake": FAKE_MODEL_RE}[engine]  # fmt: skip
    if not isinstance(model, str) or not pattern.fullmatch(model):
        raise JobError(400, f"invalid model name for {engine}")
    if engine == "ollama" and ":" not in model.rsplit("/", 1)[-1]:
        model += ":latest"  # how Ollama names it in /api/tags and /api/ps
    if engine == "ollama" and not (
        isinstance(revision, str) and OLLAMA_DIGEST_RE.fullmatch(revision)
    ):
        raise JobError(400, "ollama models need a pinned revision: the manifest digest (hex)")
    if engine in ("vllm", "mlx") and not (
        isinstance(revision, str) and HF_REVISION_RE.fullmatch(revision)
    ):
        raise JobError(400, f"{engine} models need a pinned revision: a 40-hex commit")
    out: dict[str, Any] = {
        "service_id": sid,
        "engine": engine,
        "model": model,
        "revision": revision if engine != "fake" else (revision or None),
        "context_length": _int(spec, "context_length", 8192, 512, 262144),
        "parallel": _int(spec, "parallel", 4, 1, 64),
        "kv_cache_type": spec.get("kv_cache_type", "q8_0"),
        "memory_gb": _number(spec, "memory_gb", None, 0.01, 1024),
        "startup_timeout_s": _int(spec, "startup_timeout_s", 1800, 5, 7200),
        "trust_remote_code": spec.get("trust_remote_code", False),
        "fake": None,
    }
    if out["kv_cache_type"] not in KV_CACHE_TYPES:
        raise JobError(400, f"kv_cache_type must be one of {list(KV_CACHE_TYPES)}")
    if engine == "vllm" and out["kv_cache_type"] == "q4_0":
        raise JobError(400, "vllm has no q4_0 KV cache (use f16 or q8_0)")
    if not isinstance(out["trust_remote_code"], bool):
        raise JobError(400, "trust_remote_code must be true or false")
    if out["trust_remote_code"] and engine != "vllm":
        raise JobError(400, "trust_remote_code only applies to vllm")
    if engine == "mlx" and out["memory_gb"] > MLX_MAX_GB:
        raise JobError(400, f"Mac models are small: at most {MLX_MAX_GB:.0f} GB")
    fake = spec.get("fake")
    if fake is not None:
        if engine != "fake" or not isinstance(fake, dict):
            raise JobError(400, "'fake' settings only apply to the fake engine")
        out["fake"] = {
            "startup_delay_s": _number(fake, "startup_delay_s", 0.0, 0.0, 600),
            "hold_memory_mb": _int(fake, "hold_memory_mb", 0, 0, 1 << 20),
            "crash_after_s": _number(fake, "crash_after_s", 0.0, 0.0, 86400),
            "unhealthy_after_s": _number(fake, "unhealthy_after_s", 0.0, 0.0, 86400),
            "ignore_sigterm": _bool(fake, "ignore_sigterm"),
            "reply_delay_s": _number(fake, "reply_delay_s", 0.0, 0.0, 600),
            "fail_status": _int(fake, "fail_status", 0, 0, 599),
            "stream_cut_after": _int(fake, "stream_cut_after", 0, 0, 1000),
            "mac_gated": _bool(fake, "mac_gated"),  # tests: gate it like a Mac model
            "reply_text": _text(fake, "reply_text", 20000),  # tests: a canned answer
        }
        if out["fake"]["hold_memory_mb"] > out["memory_gb"] * 1024:
            raise JobError(400, "the fake engine can't hold more memory than memory_gb declares")
    if (
        engine == "fake"
        and revision is not None
        and not (isinstance(revision, str) and OLLAMA_DIGEST_RE.fullmatch(revision))
    ):
        raise JobError(400, "revision must be hex")
    return out


def _int(d: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = d.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise JobError(400, f"{key} must be an integer in [{lo}, {hi}]")
    return value


def _number(d: dict[str, Any], key: str, default: Optional[float], lo: float, hi: float) -> float:
    value = d.get(key, default)
    if value is None:
        raise JobError(400, f"{key} is required")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise JobError(400, f"{key} must be a number in [{lo}, {hi}]")
    return float(value)


def _text(d: dict[str, Any], key: str, limit: int) -> Optional[str]:
    value = d.get(key)
    if value is not None and (not isinstance(value, str) or len(value) > limit):
        raise JobError(400, f"{key} must be text of at most {limit} characters")
    return value


def _bool(d: dict[str, Any], key: str) -> bool:
    value = d.get(key, False)
    if not isinstance(value, bool):
        raise JobError(400, f"{key} must be true or false")
    return value


# -- engines ------------------------------------------------------------------------


# What a service's supervisor, engine and download may see of the worker's environment:
# never the worker's token, nor switches that send downloads elsewhere
# (HF_ENDPOINT, MLXLM_USE_MODELSCOPE).
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "SHELL", "TERM",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
             "LD_LIBRARY_PATH", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")  # fmt: skip
ENV_PREFIXES = ("LC_", "CUDA_", "NVIDIA_", "NEWTON_SERVICE_", "NEWTON_MAC_GATE")


def service_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k in ENV_ALLOW or k.startswith(ENV_PREFIXES)}  # fmt: skip


def engine_executable(engine: str) -> Optional[str]:
    if engine == "fake":
        return sys.executable
    if engine == "mlx":
        # mlx-lm in the worker's own Python (find_spec doesn't import it: the worker
        # itself stays stdlib-only), on Apple Silicon only.
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return None
        wanted = ("mlx_lm", "huggingface_hub")
        return sys.executable if all(importlib.util.find_spec(m) for m in wanted) else None
    name = {"ollama": "ollama", "vllm": "vllm"}[engine]
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path(sys.executable).parent / name, Path("/usr/local/bin") / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def engine_command(
    spec: dict[str, Any], port: int, root: Path, total_memory: Optional[int] = None
) -> tuple[list[str], dict[str, str]]:
    """(argv, extra environment) for a validated spec. Fixed shapes only."""
    exe = engine_executable(spec["engine"])
    if exe is None:
        raise JobError(422, f"the {spec['engine']} engine is not installed on this host")
    models = root / "models" / spec["engine"]
    if spec["engine"] == "fake":
        fake = spec.get("fake") or {}
        argv = [exe, "-m", "newton_worker.fake_openai", "--port", str(port),
                "--model", spec["model"],
                "--startup-delay", str(fake.get("startup_delay_s", 0.0)),
                "--hold-memory-mb", str(fake.get("hold_memory_mb", 0)),
                "--crash-after", str(fake.get("crash_after_s", 0.0)),
                "--unhealthy-after", str(fake.get("unhealthy_after_s", 0.0)),
                "--reply-delay", str(fake.get("reply_delay_s", 0.0)),
                "--fail-status", str(fake.get("fail_status", 0)),
                "--stream-cut-after", str(fake.get("stream_cut_after", 0))]  # fmt: skip
        if fake.get("ignore_sigterm"):
            argv.append("--ignore-sigterm")
        if fake.get("reply_text") is not None:  # through a file: never on the command line
            reply = root / "services" / spec["service_id"] / "reply.txt"
            reply.parent.mkdir(parents=True, exist_ok=True)
            reply.write_text(fake["reply_text"], encoding="utf-8")
            argv += ["--reply-file", str(reply)]
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        return argv, {"PYTHONPATH": pkg_parent}
    if spec["engine"] == "ollama":
        # A Newton-owned server: its own port, model store and settings (the host's
        # system Ollama, if any, is left alone).
        return [exe, "serve"], {
            "OLLAMA_HOST": f"127.0.0.1:{port}",
            "OLLAMA_MODELS": str(models),
            "OLLAMA_NUM_PARALLEL": str(spec["parallel"]),
            "OLLAMA_CONTEXT_LENGTH": str(spec["context_length"]),
            "OLLAMA_KV_CACHE_TYPE": spec["kv_cache_type"],
            "OLLAMA_FLASH_ATTENTION": "1",
            "OLLAMA_KEEP_ALIVE": "-1",  # resident until the service stops
            "OLLAMA_MAX_LOADED_MODELS": "1",
        }
    if spec["engine"] == "mlx":
        # mlx-lm's server on the exact snapshot fetched before it starts. It has no API
        # key, so no browser origin may call it either.
        argv = [exe, "-m", "mlx_lm", "server", "--model", str(mlx_snapshot(spec, root)),
                "--host", "127.0.0.1", "--port", str(port),
                "--allowed-origins", "http://127.0.0.1:1",
                "--decode-concurrency", str(spec["parallel"]),
                "--prompt-concurrency", str(min(spec["parallel"], 4)),
                # Bounded caches: a quarter of the declared memory for reused prompts.
                "--prompt-cache-size", "4",
                "--prompt-cache-bytes", str(int(float(spec["memory_gb"]) * GIB / 4)),
                "--log-level", "WARNING"]  # fmt: skip
        bits = {"q8_0": "8", "q4_0": "4"}.get(spec["kv_cache_type"])
        if bits:
            argv += ["--kv-bits", bits]
        return argv, {
            "HF_HUB_CACHE": str(models), "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            # One Mac, one process: never MPI. (MLX otherwise loads any libmpi it finds,
            # e.g. a conda MPICH, and aborts because it isn't Open MPI.)
            "MLX_MPI_LIBNAME": "libnewton-no-mpi.dylib",
        }  # fmt: skip
    argv = [exe, "serve", spec["model"], "--revision", spec["revision"],
            "--host", "127.0.0.1", "--port", str(port),
            "--max-model-len", str(spec["context_length"]),
            "--max-num-seqs", str(spec["parallel"]),
            "--kv-cache-dtype", "auto" if spec["kv_cache_type"] == "f16" else "fp8",
            "--gpu-memory-utilization", f"{vllm_utilization(spec, total_memory):.3f}"]  # fmt: skip
    if spec["trust_remote_code"]:
        argv.append("--trust-remote-code")
    # The cache only: HF_HOME would also hide the host's Hugging Face login token.
    return argv, {"HF_HUB_CACHE": str(models)}


def mlx_snapshot(spec: dict[str, Any], root: Path) -> Path:
    """Where the pinned commit lands: the Hugging Face cache layout, so the path names
    the exact revision that was approved."""
    repo = "models--" + str(spec["model"]).replace("/", "--")
    return root / "models" / "mlx" / repo / "snapshots" / str(spec["revision"])


def fetch_command(spec: dict[str, Any], root: Path) -> Optional[list[str]]:
    """The download step before the engine starts (MLX: the pinned snapshot)."""
    if spec["engine"] != "mlx":
        return None
    exe = engine_executable("mlx")
    if exe is None:
        raise JobError(422, "mlx-lm is not installed on this Mac")
    return [exe, "-m", "huggingface_hub.cli.hf", "download", spec["model"],
            "--revision", spec["revision"], "--cache-dir", str(root / "models" / "mlx"),
            *[arg for pattern in MLX_FILES for arg in ("--include", pattern)],
            "--quiet"]  # fmt: skip


def mac_gate() -> dict[str, Any]:
    """Mac models run only on AC power and without memory pressure: a laptop on
    battery, or one already swapping, isn't a place for a model server.
    NEWTON_MAC_GATE, or the file NEWTON_MAC_GATE_FILE names (tests: it can change
    while a service runs), can only close the gate: "battery" or "pressure"."""
    forced = os.environ.get("NEWTON_MAC_GATE")
    gate_file = os.environ.get("NEWTON_MAC_GATE_FILE")
    if gate_file:
        try:
            forced = Path(gate_file).read_text().strip() or forced
        except OSError:
            pass
    ac: Optional[bool] = None
    pressure: Optional[int] = None
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                             timeout=5).stdout  # fmt: skip
        first = out.splitlines()[0] if out else ""
        ac = True if "AC Power" in first else False if "Battery Power" in first else None
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        argv = ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]
        level = subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout.strip()
        pressure = int(level) if level.isdigit() else None
    except (OSError, subprocess.SubprocessError):
        pass
    if forced == "battery":
        ac = False
    if forced == "pressure":
        pressure = 4
    reasons = []
    if ac is False:
        reasons.append("this Mac is on battery power")
    # 1 normal, 2 warning, 4 critical. "Warning" is common on a busy Mac with plenty
    # free (admission checks real free memory anyway): only critical closes the gate.
    if pressure is not None and pressure >= 4:
        reasons.append("this Mac is under memory pressure")
    return {"ok": not reasons, "ac_power": ac, "memory_pressure": pressure,
            "reason": "; ".join(reasons) or None}  # fmt: skip


def gated(spec: dict[str, Any]) -> bool:
    return spec["engine"] == "mlx" or bool((spec.get("fake") or {}).get("mac_gated"))


def vllm_utilization(spec: dict[str, Any], total_memory: Optional[int]) -> float:
    """vLLM takes --gpu-memory-utilization x total memory up front (on a GB10 that is
    system RAM): derive it from the declared memory, so admission counts what vLLM
    will really take."""
    if not total_memory:
        raise JobError(409, "can't size vLLM: the host's total memory is unknown")
    fraction = float(spec["memory_gb"]) * GIB / total_memory
    if fraction > 0.9:
        raise JobError(409, f"{spec['memory_gb']} GB is more than vLLM may take on this host")
    return max(0.05, fraction)


# -- memory admission -------------------------------------------------------------------


def memory_now() -> dict[str, Optional[int]]:
    """Total and available RAM in bytes (Linux /proc/meminfo; macOS vm_stat)."""
    total: Optional[int] = None
    available: Optional[int] = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            fields = rest.split()
            if not fields or not fields[0].isdigit():
                continue
            if key == "MemTotal":
                total = int(fields[0]) * 1024
            elif key == "MemAvailable":
                available = int(fields[0]) * 1024
    elif sys.platform == "darwin":
        try:
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                       text=True, timeout=5).stdout.strip())  # fmt: skip
            stat = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", stat).group(1))  # type: ignore[union-attr]
            pages = {k.strip(): int(v.strip(" .")) for k, v in
                     (ln.split(":", 1) for ln in stat.splitlines()[1:] if ":" in ln)
                     if v.strip(" .").isdigit()}  # fmt: skip
            free = sum(
                pages.get(k, 0)
                for k in ("Pages free", "Pages inactive", "Pages purgeable", "Pages speculative")
            )
            available = free * page
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
    return {"total": total, "available": available}


def reserve_bytes(total: Optional[int]) -> int:
    """Kept free for the system and the GPU's own needs: 8 GiB or 10%, the larger."""
    override = os.environ.get("NEWTON_SERVICE_RESERVE_GB")
    if override:
        value = float(override)
        if not 0 <= value < 1e6:
            raise JobError(500, "NEWTON_SERVICE_RESERVE_GB must be a number of GB >= 0")
        return int(value * GIB)
    if sys.platform == "darwin":  # a Mac's own apps live in this memory: less to keep
        return max(3 * GIB, int(0.08 * (total or 0)))
    return max(8 * GIB, int(0.10 * (total or 0)))


DISK_RESERVE = 10 * GIB  # never fill the disk the system, the worker and its logs live on


def model_present(spec: dict[str, Any], root: Path) -> bool:
    store = root / "models" / spec["engine"]
    if spec["engine"] == "ollama":
        name = spec["model"]
        repo, tag = name.rsplit(":", 1)
        parts = repo.split("/") if "/" in repo else ["library", repo]
        manifest: Path = store / "manifests" / "registry.ollama.ai" / Path(*parts) / tag
        return manifest.is_file()
    if spec["engine"] == "vllm":
        cache: Path = store / ("models--" + str(spec["model"]).replace("/", "--"))
        return cache.is_dir()
    if spec["engine"] == "mlx":  # written only after a complete, size-checked download
        return (mlx_snapshot(spec, root) / MLX_COMPLETE).is_file()
    return True


# -- the store ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def supervisor_alive(pid: Any, service_dir: Path) -> Optional[bool]:
    """Is `pid` this service's supervisor? None: can't tell right now (a `ps`
    hiccup must not get a healthy service declared lost and its engine killed)."""
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    is_it = last_argument_is(pid, service_dir)
    if not is_it:
        return is_it
    return "newton_worker.run_service" in process_command_line(pid)


class ServiceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / "services"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def service_dir(self, service_id: str) -> Path:
        if not SERVICE_ID_RE.fullmatch(service_id):
            raise JobError(400, "invalid service_id")
        return self.dir / service_id

    def _existing(self, service_id: str) -> Path:
        d = self.service_dir(service_id)
        if not (d / "status.json").exists():
            raise JobError(404, f"unknown service {service_id}")
        return d

    def admission(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Would starting this service keep the host's reserve free?

        Services that haven't taken their memory yet (starting, loading, or stopping
        before they were ever ready: their engine may still be allocating) count by
        what they declared. States are read before memory, so a service that becomes
        ready in between is counted twice, never zero times. Unknown memory refuses."""
        pending = 0.0
        for d in self._dirs():
            raw = self._observe(d)
            if raw["state"] in ("starting", "loading") or (
                raw["state"] in ("stopping", "draining") and not raw.get("ready_at")
            ):
                pending += float(raw.get("memory_gb") or 0) * GIB
        mem = memory_now()
        need = float(spec["memory_gb"]) * GIB
        reserve = reserve_bytes(mem["total"])
        available = mem["available"]
        out: dict[str, Any] = {
            "ok": available is not None and available - pending - need >= reserve,
            "need_gb": round(need / GIB, 2),
            "available_gb": None if available is None else round(available / GIB, 2),
            "pending_gb": round(pending / GIB, 2),
            "reserve_gb": round(reserve / GIB, 2),
            "total_bytes": mem["total"],
        }
        out["model_present"] = model_present(spec, self.root)
        # Would it start at all? Checked before anyone is asked to approve it.
        out["engine_installed"] = engine_executable(spec["engine"]) is not None
        if gated(spec):
            gate = mac_gate()
            out["mac_gate"] = gate
            if not gate["ok"]:
                out.update(ok=False, sizing=f"not now: {gate['reason']}")
        if spec["engine"] == "vllm" and mem["total"]:
            try:
                vllm_utilization(spec, mem["total"])
            except JobError as e:
                out.update(ok=False, sizing=e.message)
        if not out["model_present"]:
            free = shutil.disk_usage(self.root).free
            out["disk_free_gb"] = round(free / GIB, 1)
            # The download is about the size of the model's weights (memory_gb is that
            # plus the KV cache, so it errs on the safe side).
            if free - need < DISK_RESERVE:
                out.update(ok=False, disk=False)
        return out

    def declared_memory(self) -> int:
        """Bytes declared by every service whose engine may hold memory."""
        return int(
            sum(
                float(raw.get("memory_gb") or 0) * GIB
                for raw in (self._observe(d) for d in self._dirs())
                if raw["state"] not in TERMINAL
            )
        )

    def _dirs(self) -> list[Path]:
        return [d for d in sorted(self.dir.iterdir()) if (d / "status.json").exists()]

    def create(self, raw: Any, api_key: Optional[str] = None) -> dict[str, Any]:
        """Start a service. `api_key`: the key agentd generated and already stored
        (so a retried request can't lose it); without one the worker makes one and
        returns it once."""
        spec = validate_spec(raw)
        if api_key is not None and not API_KEY_RE.fullmatch(api_key):
            raise JobError(400, "invalid api key")
        with self._lock:
            d = self.service_dir(spec["service_id"])
            if (d / "status.json").exists():
                current = self._refresh(d)
                if current["state"] not in TERMINAL:
                    if read_json(d / "spec.json") != spec:
                        raise JobError(409, "service_id is running with a different spec")
                    return current  # a retried request: the same service
                # Stopped, failed or lost: the id may start again, with this spec.
            admission = self.admission(spec)
            if admission.get("disk") is False:
                raise JobError(
                    409,
                    f"not enough disk to download {spec['model']}: about {admission['need_gb']} "
                    f"GB needed, {admission['disk_free_gb']} GB free, and "
                    f"{DISK_RESERVE // GIB} GB is kept free",
                )
            if admission.get("mac_gate") and not admission["mac_gate"]["ok"]:
                # Not now (on battery, memory pressure): agentd waits and asks again.
                raise JobError(503, f"not now: {admission['mac_gate']['reason']}")
            if admission.get("sizing"):
                raise JobError(409, str(admission["sizing"]))
            if admission["available_gb"] is None:
                raise JobError(409, "can't read this host's available memory: not starting")
            if not admission["ok"]:
                raise JobError(
                    409,
                    f"not enough memory to start {spec['model']}: it needs "
                    f"{admission['need_gb']} GB, {admission['available_gb']} GB is available "
                    f"({admission['pending_gb']} GB promised to services still loading), and "
                    f"{admission['reserve_gb']} GB is kept free for the system",
                )
            port = free_port()
            argv, env = engine_command(spec, port, self.root, admission["total_bytes"])
            # A per-service API key where the engine can check one (vLLM, the fake):
            # handed to the engine in its environment (never argv, which `ps` shows),
            # and returned once, in this response, for agentd's Keychain.
            generated = None
            if spec["engine"] in API_KEY_ENV:
                if api_key is None:
                    api_key = generated = secrets.token_urlsafe(32)
                env[API_KEY_ENV[spec["engine"]]] = api_key
            d.mkdir(parents=True, exist_ok=True)
            (d / "stop").unlink(missing_ok=True)
            (d / "control.json").unlink(missing_ok=True)
            write_json_atomic(d / "spec.json", spec)
            write_json_atomic(
                d / "command.json",
                {"argv": argv, "env": env, "fetch": fetch_command(spec, self.root)},
            )
            status = {
                "service_id": spec["service_id"],
                "engine": spec["engine"],
                "model": spec["model"],
                "state": "starting",
                "port": port,
                "memory_gb": spec["memory_gb"],
                "admission": admission,
                "created_at": time.time(),
                "ready_at": None,
                "finished_at": None,
                "supervisor_pid": None,
                "engine_pid": None,
                "healthy": None,
                "error": None,
            }
            write_json_atomic(d / "status.json", status)
            log = open(d / "supervisor.log", "ab")  # noqa: SIM115 - handed to the child
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "newton_worker.run_service", str(d)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    close_fds=True,
                    env={**service_env(),
                         "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
                )  # fmt: skip
            finally:
                log.close()
            status["supervisor_pid"] = proc.pid
            write_json_atomic(d / "status.json", status)
            return {**status, "api_key": generated} if generated else status

    def list(self) -> list[dict[str, Any]]:
        return [self._refresh(d) for d in self._dirs()]

    def status(self, service_id: str) -> dict[str, Any]:
        return self._refresh(self._existing(service_id))

    def stop(self, service_id: str) -> dict[str, Any]:
        d = self._existing(service_id)
        status = read_json(d / "status.json")  # raw: the request must be on disk first
        if status["state"] in TERMINAL:
            return status
        (d / "stop").touch()  # the supervisor also polls for this file
        alive = supervisor_alive(status.get("supervisor_pid"), d)
        if alive:
            os.kill(status["supervisor_pid"], signal.SIGTERM)
        elif alive is False:  # nobody is watching the engine any more: stop it ourselves
            raw = read_json(d / "status.json")
            stop_group(raw.get("fetch_pid"), raw.get("fetch_identity"))
            stop_group(raw.get("engine_pid"), raw.get("engine_identity"))
            raw.update(state="stopped", finished_at=time.time())
            write_json_atomic(d / "status.json", raw)
        return self._refresh(d)

    def drain(self, service_id: str, seconds: float) -> dict[str, Any]:
        """Stop taking new work (the router reads `draining`), then stop after
        `seconds` so requests already in flight can finish."""
        d = self._existing(service_id)
        if not 0 <= seconds <= 86400:
            raise JobError(400, "drain seconds must be in [0, 86400]")
        status = self._refresh(d)
        if status["state"] in TERMINAL or status["state"] == "stopping":
            return status
        write_json_atomic(d / "control.json", {"drain_until": time.time() + seconds})
        return self._refresh(d)

    def logs(self, service_id: str, offset: int, limit: int) -> dict[str, Any]:
        d = self._existing(service_id)
        path = d / "engine.log"
        if offset < 0 or not 0 < limit <= 1 << 20:
            raise JobError(400, "bad offset or limit")
        if not path.exists():
            return {"offset": 0, "next_offset": 0, "data": "", "eof": True}
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(limit)
        return {
            "offset": offset,
            "next_offset": offset + len(data),
            "data": data.decode("utf-8", errors="replace"),
            "eof": offset + len(data) >= path.stat().st_size,
        }

    def _refresh(self, d: Path) -> dict[str, Any]:
        status = self._observe(d)
        # Requests the supervisor hasn't acted on yet still show: the caller (and the
        # router) must not send new work to a service that was asked to stop or drain.
        if (
            status["state"] in ("starting", "loading", "ready", "draining")
            and (d / "stop").exists()
        ):
            status = {**status, "state": "stopping"}
        elif status["state"] == "ready" and (d / "control.json").exists():
            status = {**status, "state": "draining"}
        return status

    def _observe(self, d: Path) -> dict[str, Any]:
        status = read_json(d / "status.json")
        if status["state"] in TERMINAL:
            return status
        alive = supervisor_alive(status.get("supervisor_pid"), d)
        if alive or alive is None:
            return status
        # Started moments ago, before the supervisor recorded itself? Give it time.
        if status["state"] == "starting" and time.time() - float(status.get("created_at") or 0) < 5:
            return status
        status = read_json(d / "status.json")  # it may have just finished
        if status["state"] in TERMINAL:
            return status
        stop_group(status.get("fetch_pid"), status.get("fetch_identity"))  # a download, too
        stop_group(status.get("engine_pid"), status.get("engine_identity"))
        if (d / "stop").exists():
            # Asked to stop, and now stopped (by us, just above, if it had an engine).
            status.update(state="stopped", finished_at=time.time())
        else:
            status.update(state="lost", finished_at=time.time(),
                          error="the service supervisor is gone (host reboot?)")  # fmt: skip
        write_json_atomic(d / "status.json", status)
        return status
