// Where each variant ran, and the provenance agentd recorded.

import type { ValidationReport } from "../../api";
import { StateChip } from "../../components/ui";
import { when } from "../../components/time";

function text(v: unknown): string {
  if (v === null || v === undefined) return "—";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : JSON.stringify(v);
}

const LABELS: Record<string, string> = {
  repository_commit: "Repository commit",
  worker_version: "Worker",
  host_id: "Host",
  generated_at: "Report generated",
};

export function Provenance({ report }: { report: ValidationReport }) {
  const p = report.provenance ?? {};
  const jobs = p.jobs && typeof p.jobs === "object" ? (p.jobs as Record<string, Record<string, unknown> | null>) : null;
  const rest = Object.entries(p).filter(([k]) => k !== "jobs");
  return (
    <div className="ex-two">
      <div className="card">
        <div className="h-card" style={{ marginBottom: 10 }}>
          Variants
        </div>
        <div className="ex-table-wrap">
          <table className="ex-table">
            <thead>
              <tr>
                <th>Variant</th>
                <th>Backend</th>
                <th>Device</th>
                <th>Job</th>
              </tr>
            </thead>
            <tbody>
              {report.variants.map((v) => (
                <tr key={v.job_id || v.label}>
                  <td>
                    {v.label} <span className="muted small">{v.role}</span>
                  </td>
                  <td>{v.backend ?? "—"}</td>
                  <td>{v.device ?? "—"}</td>
                  <td>
                    <StateChip kind="job" state={v.job_state} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="card">
        <div className="h-card" style={{ marginBottom: 10 }}>
          Provenance
        </div>
        <div className="ex-prov">
          {rest.map(([k, v]) => (
            <div className="ex-prov-row" key={k}>
              <span className="muted">{LABELS[k] ?? k.replace(/_/g, " ")}</span>
              <span className={k === "generated_at" ? "" : "mono"}>
                {k === "generated_at" && typeof v === "number" ? when(v) : text(v)}
              </span>
            </div>
          ))}
          {jobs
            ? Object.entries(jobs).map(([label, j]) => (
                <div className="ex-prov-row" key={`job-${label}`}>
                  <span className="muted">Job · {label}</span>
                  <span>
                    <span className="mono">{text(j?.id)}</span>
                    {j?.attempt !== undefined ? (
                      <span className="muted small"> · attempt {text(j.attempt)}</span>
                    ) : null}
                    {j?.device_lease ? <div className="mono small muted">lease {text(j.device_lease)}</div> : null}
                  </span>
                </div>
              ))
            : null}
        </div>
      </div>
    </div>
  );
}
