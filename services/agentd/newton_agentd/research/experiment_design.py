"""Compile a typed ExperimentSpec into jobs, gated by an approval."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..contracts import ExperimentSpec, Variant
from ..orchestration.approvals import Approvals
from ..orchestration.capabilities import place
from ..orchestration.hosts import host_view
from ..orchestration.jobs import create_job, job_view, request_cancel
from ..orchestration.state_machine import EXPERIMENT, JOB, ConcurrentTransition
from ..runners.bundle import build_bundle, kernel_hashes, repository_commit
from ..storage.db import Database, Row, dumps, loads, new_id, now
from . import schemes
from .estimate import MEMORY_SHARE, estimate

APPROVAL_KIND = "execute_experiment"

ADVECTION_METRICS = [
    "l2_error",
    "linf_error",
    "observed_order",
    "runtime",
    "conservation",
    "tv_increase",
    "max_overshoot",
    "stable",
    "time_per_step",
    "cell_updates_per_s",
    "achieved_gbps",
    "peak_gbps",
    "bandwidth_fraction",
    "speedup_vs_numpy",
    "reference_agreement",
    "agrees_with_numpy",
]
BENCHMARKS: dict[str, dict[str, Any]] = {
    "linear_advection_1d": {
        "module": "benchmarks.advection",
        "dims": 1,
        "description": "1D periodic linear advection: grid refinement, or speed on one grid",
        "metrics": ADVECTION_METRICS,
        "artifact_paths": ["results.json", "plots/"],
    },
    "linear_advection_2d": {
        "module": "benchmarks.advection",
        "dims": 2,
        "description": "2D periodic linear advection (dimension-split sweeps): grid "
        "refinement, or speed on one large grid; GPU-scale",
        "metrics": ADVECTION_METRICS,
        "artifact_paths": ["results.json", "plots/"],
    },
}


class ExperimentNotFound(KeyError):
    pass


def compile_variant(
    settings: Settings, spec: ExperimentSpec, variant: Variant, commit: str | None
) -> tuple[dict[str, Any], bytes]:
    bench = BENCHMARKS[spec.benchmark]
    manifest = {
        "job_id": "placeholder",
        "repository_commit": commit,
        "backend": spec.backend,
        "benchmark": spec.benchmark,
        # Fixed argv: all variable input travels as params.json in the bundle.
        "command": ["python", "-m", bench["module"], "--params", "params.json"],
        "timeout_seconds": spec.timeout_seconds,
        "metrics": bench["metrics"],
        "artifact_paths": bench["artifact_paths"],
        "exclusive": spec.objective == "performance",
    }
    params = {**variant.params.model_dump(), "dims": bench["dims"]}
    ir = None
    if variant.params.scheme == "ir":
        ir = schemes.check(settings.benchmarks_dir, params["scheme_ir"])
        params["scheme_ir"] = ir["document"]
        manifest["scheme_ir_digest"] = ir["digest"]
    if variant.params.implementation == "kernel" and settings.benchmarks_dir is not None:
        # What was approved is what runs: results carry the hashes the job computed
        # (for a generated kernel, the hash of the exact code generated from the IR).
        manifest["kernel_hashes"] = kernel_hashes(settings.benchmarks_dir)
        if ir is not None:
            manifest["kernel_hashes"][ir["header"]] = ir["header_hash"]
    bundle = build_bundle(settings.benchmarks_dir, {"params.json": params})
    return manifest, bundle


def experiment_view(row: Row) -> dict[str, Any]:
    out = dict(row)
    out["spec"] = loads(row["spec"])
    out["evaluation"] = loads(row["evaluation"])
    return out


class Experiments:
    def __init__(self, settings: Settings, db: Database, approvals: Approvals) -> None:
        self.settings = settings
        self.db = db
        self.approvals = approvals
        approvals.register(APPROVAL_KIND, self._on_decision)

    def create(self, spec: ExperimentSpec) -> dict[str, Any]:
        hosts = [
            host_view(r)
            for r in self.db.query(
                "SELECT * FROM hosts WHERE status != 'deleted' ORDER BY created_at"
            )
        ]
        host, backend, placement = place(
            hosts, spec.host_id, spec.backend, spec.uses_float64, spec.max_cells, spec.needs_gpu
        )
        # The stored spec, the jobs and the approval all carry the concrete choice.
        spec = spec.model_copy(update={"host_id": host["id"], "backend": backend})
        expected = estimate(self.db, spec, host["id"], backend)
        if expected["slowest_job_seconds"] > 3 * spec.timeout_seconds:
            raise ValueError(
                f"a job would take about {expected['slowest_job_seconds']:.0f} s "
                f"({expected['basis']}), far over its {spec.timeout_seconds} s timeout: use "
                "smaller grids, fewer steps, or raise timeout_seconds"
            )
        ram = ((host.get("hardware") or {}).get("memory") or {}).get("total")
        if ram and expected["peak_bytes"] > MEMORY_SHARE * ram:
            raise ValueError(
                f"a job would need about {expected['peak_bytes'] / 2**30:.1f} GB of memory, "
                f"more than {MEMORY_SHARE:.0%} of {host['name']}'s {ram / 2**30:.0f} GB: "
                "use smaller grids, float32, or the kernel implementation"
            )
        if spec.goal_id and not self.db.query_one(
            "SELECT 1 FROM goals WHERE id = ?", (spec.goal_id,)
        ):
            raise ValueError(f"unknown goal {spec.goal_id}")
        exp_id = new_id("exp")
        commit = repository_commit(self.settings.resources_dir)
        compiled = [(v, *compile_variant(self.settings, spec, v, commit)) for v in spec.variants]
        t = now()
        with self.db.tx():
            self.db.insert(
                "experiments",
                {
                    "id": exp_id,
                    "goal_id": spec.goal_id,
                    "research_item_id": spec.research_item_id,
                    "host_id": spec.host_id,
                    "title": spec.title,
                    "spec": dumps(spec.model_dump()),
                    "state": "awaiting_approval",
                    "created_at": t,
                    "updated_at": t,
                },
            )
            for variant, manifest, bundle in compiled:
                create_job(
                    self.db,
                    self.settings,
                    host_id=spec.host_id,
                    role=variant.role,
                    label=variant.label,
                    manifest=manifest,
                    bundle=bundle,
                    experiment_id=exp_id,
                    state="pending_approval",
                )
            self.approvals.request(
                APPROVAL_KIND,
                "experiment",
                exp_id,
                f"Run experiment: {spec.title}",
                {
                    "host": {"id": host["id"], "name": host["name"], "kind": host["kind"]},
                    "backend": spec.backend,
                    "device": host["capabilities"][spec.backend].get("device"),
                    "placement": placement,
                    "benchmark": spec.benchmark,
                    "objective": spec.objective,
                    "estimated_seconds": expected["seconds"],
                    "estimated_peak_gb": round(expected["peak_bytes"] / 2**30, 2),
                    "estimate_basis": expected["basis"],
                    "hypothesis": spec.hypothesis,
                    "variants": [
                        {
                            "label": v.label,
                            "role": v.role,
                            "params": v.params.model_dump(),
                            **(
                                {"scheme_ir_digest": m["scheme_ir_digest"]}
                                if "scheme_ir_digest" in m
                                else {}
                            ),
                        }
                        for v, m, _ in compiled
                    ],
                    "timeout_seconds": spec.timeout_seconds,
                    "repository_commit": commit,
                },
            )
        return self.get(exp_id)

    def _on_decision(self, db: Database, approval: Row, approved: bool) -> None:
        exp_id = approval["subject_id"]
        jobs = db.query(
            "SELECT id FROM jobs WHERE experiment_id = ? AND state = 'pending_approval'", (exp_id,)
        )
        if approved:
            EXPERIMENT.transition(db, exp_id, "awaiting_approval", "executing")
            for j in jobs:
                JOB.transition(db, j["id"], "pending_approval", "queued")
        else:
            EXPERIMENT.transition(db, exp_id, "awaiting_approval", "rejected")
            for j in jobs:
                JOB.transition(db, j["id"], "pending_approval", "rejected", {"finished_at": now()})

    def get_row(self, exp_id: str) -> Row:
        row = self.db.query_one("SELECT * FROM experiments WHERE id = ?", (exp_id,))
        if row is None:
            raise ExperimentNotFound(exp_id)
        return row

    def get(self, exp_id: str) -> dict[str, Any]:
        out = experiment_view(self.get_row(exp_id))
        out["jobs"] = [
            job_view(r)
            for r in self.db.query(
                "SELECT * FROM jobs WHERE experiment_id = ? ORDER BY created_at, role", (exp_id,)
            )
        ]
        approval = self.db.query_one(
            "SELECT id, status FROM approvals WHERE subject_type = 'experiment' AND subject_id = ?",
            (exp_id,),
        )
        out["approval"] = approval
        return out

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.db.query(
                "SELECT * FROM experiments WHERE state = ? ORDER BY created_at DESC", (state,)
            )
        else:
            rows = self.db.query("SELECT * FROM experiments ORDER BY created_at DESC")
        return [experiment_view(r) for r in rows]

    def cancel(self, exp_id: str) -> dict[str, Any]:
        row = self.get_row(exp_id)
        if row["state"] not in ("awaiting_approval", "executing"):
            return self.get(exp_id)
        for j in self.db.query("SELECT id FROM jobs WHERE experiment_id = ?", (exp_id,)):
            request_cancel(self.db, j["id"])
        pending = self.db.query_one(
            "SELECT id FROM approvals WHERE subject_type = 'experiment' AND subject_id = ? "
            "AND status = 'pending'",
            (exp_id,),
        )
        if pending:
            self.db.execute(
                "UPDATE approvals SET status = 'rejected', decision_note = 'experiment cancelled', "
                "decided_at = ? WHERE id = ?",
                (now(), pending["id"]),
            )
        try:
            EXPERIMENT.transition(self.db, exp_id, row["state"], "cancelled")
        except ConcurrentTransition:
            pass
        return self.get(exp_id)
