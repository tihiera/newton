"""Typed contracts shared with the UI and the worker.

These pydantic models are the source of truth; `scripts/export_schemas.py` writes
them to `packages/contracts/*.schema.json`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

JOB_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.+/-]+$")


def _legacy_backend(value: Any) -> Any:
    return "cuda" if value == "gpu" else value  # "gpu" meant CUDA before metal existed


# Where the numbers are computed. "metal" is the Apple GPU (MLX; float32 only).
Backend = Annotated[Literal["cpu", "cuda", "metal"], BeforeValidator(_legacy_backend)]
# What an experiment may ask for; "auto" is resolved before approval.
BackendChoice = Annotated[Literal["auto", "cpu", "cuda", "metal"], BeforeValidator(_legacy_backend)]
JobState = Literal[
    "pending_approval",
    "queued",
    "submitting",
    "running",
    "collecting",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "rejected",
]
Evidence = Literal["green", "yellow", "red", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- Worker protocol ---------------------------------------------------------


class JobManifest(StrictModel):
    """What the worker executes. Never contains shell strings."""

    job_id: Annotated[str, Field(pattern=JOB_ID_PATTERN)]
    experiment_id: str | None = None
    repository_commit: str | None = None
    backend: Backend = "cpu"
    benchmark: str
    command: list[str] = Field(min_length=3)
    timeout_seconds: int = Field(default=900, ge=1, le=7 * 24 * 3600)
    metrics: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    # Timed for a speed verdict: runs alone on its host, so nothing else (not even the
    # experiment's other variants) competes for the memory bandwidth it measures.
    exclusive: bool = False
    # sha256 of the kernel sources in the bundle (bundle path -> digest): the job
    # reports what it compiled, and evidence only counts if the two match.
    kernel_hashes: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(
        default_factory=dict
    )
    # E2: the SchemeIR document that was approved (sha256 of its canonical form).
    scheme_ir_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("command")
    @classmethod
    def _python_module_only(cls, v: list[str]) -> list[str]:
        if v[0] != "python" or v[1] != "-m":
            raise ValueError("command must be ['python', '-m', <module>, ...]")
        return v


class WorkerJobStatus(BaseModel):
    job_id: str
    state: Literal[
        "created",
        "bundled",
        "starting",
        "running",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "lost",
    ]
    created_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    duration_seconds: float | None = None


# -- Benchmarks and experiments ---------------------------------------------

AdvectionScheme = Literal["upwind", "lax_wendroff", "muscl_minmod", "muscl_vanleer", "ir"]

# -- Engine E2: a scheme as data (benchmarks/advection/ir.py has the full rules) ------

IRNumber = Annotated[float, Field(allow_inf_nan=False)]


class IRExpr(StrictModel):
    """A(c) node: two arguments, each a number, "c", or another node."""

    op: Literal["add", "sub", "mul", "div"]
    args: list[IRNumber | Literal["c"] | IRExpr] = Field(min_length=2, max_length=2)


class IRFlux(StrictModel):
    limiter: Literal["none", "minmod", "van_leer", "superbee", "mc", "koren"] = "none"
    correction: IRNumber | Literal["c"] | IRExpr | None = Field(
        description="A(c) in F = u_i + A(c) φ(r) (u_{i+1} - u_i); null: first-order upwind"
    )


class IRTableau(StrictModel):
    a: list[list[IRNumber]] = Field(min_length=1, max_length=4)
    b: list[IRNumber] = Field(min_length=1, max_length=4)


class IRTime(StrictModel):
    method: Literal["one_step", "rk"] = "one_step"
    tableau: IRTableau | None = None


class IRClaims(StrictModel):
    order: int = Field(ge=1, le=4)
    max_cfl: float = Field(gt=0, le=2)
    tvd: bool = False


class SchemeIR(StrictModel):
    """A finite-volume scheme for linear advection as data: trusted generators turn it
    into numpy / CUDA / Metal code; the experiment checks its claims."""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=2000)
    flux: IRFlux
    time: IRTime = Field(default_factory=IRTime)
    claims: IRClaims


class AdvectionParams(StrictModel):
    """Parameters for benchmarks.advection (periodic linear advection, 1D or 2D)."""

    scheme: AdvectionScheme
    # Grid points per side: n cells in 1D, n x n in 2D.
    resolutions: list[Annotated[int, Field(ge=16, le=1_048_576)]] = Field(
        default_factory=lambda: [64, 128, 256, 512, 1024], min_length=1, max_length=10
    )
    mode: Literal["convergence", "throughput"] = Field(
        default="convergence",
        description="convergence: refine the grid, compare with the exact solution; "
        "throughput: a fixed number of steps on one large grid, timed",
    )
    implementation: Literal["array", "kernel"] = Field(
        default="array",
        description="array: numpy-style array operations; kernel: Newton's hand-written "
        "CUDA / Metal kernels (GPU backends only)",
    )
    steps: int = Field(default=100, ge=1, le=100_000, description="throughput mode only")
    cfl: float = Field(default=0.5, gt=0, le=1.0)
    velocity: float = Field(default=1.0, gt=0, le=100.0)
    velocity_y: float = Field(default=0.5, gt=0, le=100.0, description="2D only")
    t_final: float = Field(default=1.0, gt=0, le=100.0)
    initial_condition: Literal["gaussian", "sine", "square"] = "sine"
    precision: Literal["float64", "float32"] = "float64"
    repeats: int = Field(default=3, ge=1, le=20)
    scheme_ir: SchemeIR | None = Field(default=None, description="with scheme 'ir' only")

    @model_validator(mode="after")
    def _ir_with_ir(self) -> AdvectionParams:
        if (self.scheme == "ir") != (self.scheme_ir is not None):
            raise ValueError("scheme 'ir' goes with a scheme_ir document, and only with it")
        return self

    @model_validator(mode="after")
    def _one_grid_for_throughput(self) -> AdvectionParams:
        if self.mode == "throughput" and len(self.resolutions) != 1:
            raise ValueError("throughput mode measures one grid: give exactly one resolution")
        return self


MAX_2D_SIDE = 16384  # 268M cells: 2 GB per float64 array


class Variant(StrictModel):
    role: Literal["baseline", "candidate"]
    label: str = Field(min_length=1, max_length=80)
    params: AdvectionParams


class ExperimentSpec(StrictModel):
    """A structured experiment. The only way remote work gets created."""

    title: str = Field(min_length=1, max_length=200)
    benchmark: Literal["linear_advection_1d", "linear_advection_2d"] = "linear_advection_1d"
    objective: Literal["accuracy", "performance"] = Field(
        default="accuracy",
        description="accuracy: is the candidate more accurate (convergence runs); "
        "performance: is it faster for the same result (throughput runs on one grid)",
    )
    # "auto" picks the best connected host/backend for this spec (GB10 CUDA first,
    # then the local Apple GPU for float32 runs, then the local CPU). The stored spec
    # and the approval always show the concrete choice.
    host_id: str = "auto"
    backend: BackendChoice = "auto"
    goal_id: str | None = None
    research_item_id: str | None = None
    hypothesis: str | None = Field(default=None, max_length=2000)
    timeout_seconds: int = Field(default=900, ge=10, le=24 * 3600)
    variants: list[Variant] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _one_baseline(self) -> ExperimentSpec:
        roles = [v.role for v in self.variants]
        if roles.count("baseline") != 1:
            raise ValueError("exactly one baseline variant is required")
        labels = [v.label for v in self.variants]
        if len(set(labels)) != len(labels):
            raise ValueError("variant labels must be unique")
        if self.backend == "metal" and self.uses_float64:
            raise ValueError(
                "Apple GPUs have no float64: set precision to float32 for every variant, "
                "or use the cpu or cuda backend"
            )
        if self.backend == "cpu" and self.needs_gpu:
            raise ValueError("the kernel implementation needs a GPU backend (cuda or metal)")
        params = [v.params for v in self.variants]
        if self.dims == 2 and max(max(p.resolutions) for p in params) > MAX_2D_SIDE:
            raise ValueError(f"2D grids are at most {MAX_2D_SIDE} x {MAX_2D_SIDE}")
        wanted_mode = "throughput" if self.objective == "performance" else "convergence"
        if any(p.mode != wanted_mode for p in params):
            raise ValueError(f"the {self.objective} objective compares {wanted_mode} runs")
        # A comparison is only fair if the variants differ only in what is compared:
        # the method (scheme, implementation) for accuracy, and only the implementation
        # for speed ("faster for the same result" means the same scheme and problem).
        if self.objective == "accuracy":
            # scheme_ir: a scheme given as a document is the scheme too.
            may_vary = {"scheme", "scheme_ir", "implementation", "repeats", "steps"}
            compared = "scheme and implementation"
        else:
            may_vary = {"implementation"}
            compared = "implementation"
        differing = [
            k for k in AdvectionParams.model_fields
            if k not in may_vary and len({repr(getattr(p, k)) for p in params}) > 1
        ]  # fmt: skip
        if differing:
            article = "an" if self.objective == "accuracy" else "a"
            raise ValueError(
                f"variants differ in {', '.join(differing)}: {article} {self.objective} "
                f"comparison may only vary the {compared}"
            )
        return self

    @property
    def dims(self) -> int:
        return 2 if self.benchmark == "linear_advection_2d" else 1

    @property
    def needs_gpu(self) -> bool:
        return any(v.params.implementation == "kernel" for v in self.variants)

    @property
    def max_cells(self) -> int:
        side: int = max(max(v.params.resolutions) for v in self.variants)
        return side * side if self.dims == 2 else side

    @property
    def uses_float64(self) -> bool:
        return any(v.params.precision == "float64" for v in self.variants)


# -- Hosts -------------------------------------------------------------------


GpuSupport = Literal["auto", "off"]


class HostUpdate(StrictModel):
    gpu_support: GpuSupport | None = None
    max_parallel_jobs: int | None = Field(default=None, ge=1, le=64)


class HostCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["ssh"] = "ssh"
    ssh_target: str = Field(
        min_length=1, max_length=255, description="ssh config alias or hostname"
    )
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    python: str = Field(default="python3", max_length=255)
    use_venv: bool = True
    install_deps: bool = True
    gpu_support: GpuSupport = Field(
        default="auto",
        description="auto: install and verify CuPy when the host has an NVIDIA driver",
    )
    max_parallel_jobs: int = Field(default=2, ge=1, le=64)

    @model_validator(mode="before")
    @classmethod
    def _split_user_at_host(cls, data: Any) -> Any:
        """Accept what people type: "alice@spark.local" and "[fe80::1]"."""
        if isinstance(data, dict) and isinstance(data.get("ssh_target"), str):
            target = data["ssh_target"].strip()
            if "@" in target and not data.get("ssh_user"):
                user, _, target = target.rpartition("@")
                data = {**data, "ssh_user": user}
            # OpenSSH wants bare IPv6 literals (brackets don't resolve).
            if target.startswith("[") and target.endswith("]"):
                target = target[1:-1]
            data = {**data, "ssh_target": target}
        return data

    @field_validator("ssh_target")
    @classmethod
    def _safe_target(cls, v: str) -> str:
        if ":" in v:
            try:
                ipaddress.IPv6Address(v)
            except ValueError:
                raise ValueError("not a valid host, alias or IPv6 address") from None
            return v
        if not SAFE_TOKEN.match(v) or v.startswith("-"):
            raise ValueError("contains characters that are not allowed")
        return v

    @field_validator("ssh_user", "python")
    @classmethod
    def _safe(cls, v: str | None) -> str | None:
        if v is not None and (not SAFE_TOKEN.match(v) or v.startswith("-")):
            raise ValueError("contains characters that are not allowed")
        return v


class HostKeyTrust(StrictModel):
    fingerprints: list[str] = Field(min_length=1)


# -- Goals -------------------------------------------------------------------


# The research loop searches arXiv with these: plain words, and arXiv categories.
Keyword = Annotated[str, Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9 .+'-]+$")]
ArxivCategory = Annotated[str, Field(pattern=r"^[a-z-]+(\.[A-Za-z-]+)?$", max_length=40)]


class GoalCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    keywords: list[Keyword] = Field(default_factory=list, max_length=20)
    categories: list[ArxivCategory] = Field(
        default_factory=lambda: ["physics.comp-ph", "math.NA", "physics.flu-dyn"], max_length=10
    )
    poll_hours: float = Field(default=24, ge=1, le=24 * 30, description="how often to look")
    auto_propose: bool = Field(
        default=True, description="propose experiments for new schemes (they still need approval)"
    )


class GoalUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    keywords: list[Keyword] | None = Field(default=None, max_length=20)
    categories: list[ArxivCategory] | None = Field(default=None, max_length=10)
    poll_hours: float | None = Field(default=None, ge=1, le=24 * 30)
    auto_propose: bool | None = None
    status: Literal["active", "paused", "archived"] | None = None


class Decision(StrictModel):
    note: str | None = Field(default=None, max_length=2000)


# -- Validation report -------------------------------------------------------


class Check(BaseModel):
    name: str
    passed: bool | None
    detail: str


class VariantEvaluation(BaseModel):
    label: str
    role: Literal["baseline", "candidate"]
    job_id: str
    job_state: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[dict[str, Any]] = Field(
        default_factory=list, description="the scheme's claims against what the run measured"
    )
    backend: str | None = None  # what the job actually ran on (from its results)
    device: str | None = None


class CandidateVerdict(BaseModel):
    label: str
    evidence: Evidence
    checks: list[Check]
    summary: str


class ValidationReport(BaseModel):
    experiment_id: str
    title: str
    benchmark: str
    evidence: Evidence
    summary: str
    variants: list[VariantEvaluation]
    verdicts: list[CandidateVerdict]
    provenance: dict[str, Any]
    report_path: str | None = None


# -- Model services (SV1) ------------------------------------------------------------


class FakeEngineSettings(StrictModel):
    """Only for engine "fake": simulate a real engine's hard parts in tests."""

    startup_delay_s: float = Field(default=0.0, ge=0, le=600)
    hold_memory_mb: int = Field(default=0, ge=0, le=1 << 20)
    crash_after_s: float = Field(default=0.0, ge=0, le=86400)
    unhealthy_after_s: float = Field(default=0.0, ge=0, le=86400)
    ignore_sigterm: bool = False
    reply_delay_s: float = Field(default=0.0, ge=0, le=600)
    mac_gated: bool = False  # gate it like a Mac model (AC power, memory pressure)
    reply_text: str | None = Field(default=None, max_length=20000)  # a canned answer
    fail_status: int = Field(default=0, ge=0, le=599)
    stream_cut_after: int = Field(default=0, ge=0, le=1000)


# The worker's own patterns (services/worker/newton_worker/services.py), fully matched.
SERVICE_MODEL_PATTERNS = {
    "ollama": r"[a-z0-9][a-z0-9._-]{0,99}(/[a-z0-9][a-z0-9._-]{0,99})?(:[A-Za-z0-9._-]{1,64})?",
    "vllm": r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}",
    "mlx": r"mlx-community/[A-Za-z0-9][A-Za-z0-9._-]{0,95}-4bit(-[A-Za-z0-9._-]+)?",
    "fake": r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}",
}


class ServiceSettings(StrictModel):
    """A model server the worker runs next to jobs (OpenAI-compatible, 127.0.0.1).
    Typed fields only: no free-form engine flags. The worker validates it again
    with the same rules."""

    engine: Literal["ollama", "vllm", "mlx", "fake"]
    model: str = Field(min_length=1, max_length=200)
    # Pinned: an Ollama manifest digest (hex prefix), or a Hugging Face commit (40 hex).
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{12,64}$")
    context_length: int = Field(default=8192, ge=512, le=262144)
    parallel: int = Field(default=4, ge=1, le=64)
    kv_cache_type: Literal["f16", "q8_0", "q4_0"] = "q8_0"
    memory_gb: float = Field(
        ge=0.01, le=1024, description="what admission reserves for it (vLLM is sized from it)"
    )
    startup_timeout_s: int = Field(default=1800, ge=5, le=7200)
    trust_remote_code: bool = Field(
        default=False, description="vllm only; runs model-supplied code: needs approval"
    )
    fake: FakeEngineSettings | None = None

    @model_validator(mode="after")
    def _engine_rules(self) -> ServiceSettings:
        if not re.fullmatch(SERVICE_MODEL_PATTERNS[self.engine], self.model):
            raise ValueError(f"invalid model name for {self.engine}")
        if self.engine == "ollama" and ":" not in self.model.rsplit("/", 1)[-1]:
            self.model += ":latest"  # how Ollama names it
        if self.engine == "ollama" and not self.revision:
            raise ValueError("ollama models need a pinned revision (the manifest digest)")
        if self.engine in ("vllm", "mlx") and not (self.revision and len(self.revision) == 40):
            raise ValueError(f"{self.engine} models need a pinned revision: a 40-hex commit")
        if self.engine == "mlx" and self.memory_gb > 16:
            raise ValueError("Mac models are small: at most 16 GB")
        if self.trust_remote_code and self.engine != "vllm":
            raise ValueError("trust_remote_code only applies to vllm")
        if self.engine == "vllm" and self.kv_cache_type == "q4_0":
            raise ValueError("vllm has no q4_0 KV cache (use f16 or q8_0)")
        if self.fake is not None:
            if self.engine != "fake":
                raise ValueError("'fake' settings only apply to the fake engine")
            if self.fake.hold_memory_mb > self.memory_gb * 1024:
                raise ValueError("the fake engine can't hold more memory than memory_gb declares")
        return self


class ServiceSpec(ServiceSettings):
    """What the worker receives: the settings plus the id agentd gave the service."""

    service_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]


class ServiceCreate(StrictModel):
    """POST /services: run a model server on a host."""

    host_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=80)
    settings: ServiceSettings


class ProfileUpdate(StrictModel):
    """PATCH /profile. No accounts: one local profile."""

    display_name: str | None = Field(default=None, max_length=80)
    mac_models: bool | None = Field(
        default=None,
        description="allow model services on this Mac (MLX); off by default, and even "
        "on they run only on AC power and without memory pressure",
    )
    default_model: str | None = Field(
        default=None,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$",
        description="what model 'default' means in /v1 requests (a model, model@revision "
        "or service id); null clears it",
    )

    @field_validator("default_model")
    @classmethod
    def _resolvable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.lower() == "default":
            raise ValueError("'default' can't stand for itself")
        model, at, revision = value.partition("@")
        if at and not re.fullmatch(r"[0-9a-f]{1,64}", revision):
            raise ValueError("a pinned revision is hex: model@<revision>")
        return value


SCHEMAS: dict[str, type[BaseModel]] = {
    "scheme-ir": SchemeIR,
    "profile-update": ProfileUpdate,
    "service-spec": ServiceSpec,
    "service-create": ServiceCreate,
    "job-manifest": JobManifest,
    "job-status": WorkerJobStatus,
    "experiment": ExperimentSpec,
    "research-goal": GoalCreate,
    "host": HostCreate,
    "validation-report": ValidationReport,
}
