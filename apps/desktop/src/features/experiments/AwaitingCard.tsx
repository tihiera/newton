// awaiting_approval: what was planned, and the way to the review dialog. Approval
// is only ever given there, by the user.

import type { Experiment } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { Chip } from "../../components/ui";

export function AwaitingCard({ exp }: { exp: Experiment }) {
  const nav = useNav();
  const approval = exp.approval;
  const variants = exp.spec?.variants ?? [];
  return (
    <div className="card mesh-card ex-awaiting">
      <div className="card-head">
        <span className="icon-tile">
          <Icon name="clock" size={22} />
        </span>
        <div style={{ flex: 1 }}>
          <div className="h-card">Waiting for your approval</div>
          <div className="small muted">Nothing runs until you approve.</div>
        </div>
        {approval?.status === "pending" ? (
          <button className="btn primary" onClick={() => nav.open({ kind: "review", approvalId: approval.id })}>
            <Icon name="eye" size={18} />
            Review
          </button>
        ) : approval ? (
          <Chip>{approval.status}</Chip>
        ) : null}
      </div>
      {exp.spec?.hypothesis ? <p className="ex-hypothesis">{exp.spec.hypothesis}</p> : null}
      <div className="ex-variant-row">
        {variants.map((v) => (
          <div className="ex-variant" key={`${v.role}-${v.label}`}>
            <div className="small muted">{v.role === "baseline" ? "Baseline" : "Candidate"}</div>
            <div className="ex-variant-name">{v.label}</div>
            <div className="mono muted">
              {v.params.scheme === "ir" ? (v.params.scheme_ir?.name ?? "ir") : v.params.scheme}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
