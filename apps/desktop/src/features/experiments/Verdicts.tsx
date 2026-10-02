// Each candidate's verdict: its evidence, summary, and every check with agentd's
// detail sentence verbatim.

import type { CandidateVerdict } from "../../api";
import { Icon } from "../../components/Icon";
import { EvidenceBadge } from "../../components/ui";

function CheckIcon({ passed }: { passed: boolean | null }) {
  if (passed === true) return <Icon name="check" size={18} className="ev-green" title="Passed" />;
  if (passed === false) return <Icon name="x" size={18} className="ev-red" title="Failed" />;
  return <Icon name="info" size={18} className="ev-unknown" title="Not checked" />;
}

export function Verdicts({ verdicts }: { verdicts: CandidateVerdict[] }) {
  if (!verdicts.length) return null;
  return (
    <div className="stack" style={{ gap: 12 }}>
      {verdicts.map((v) => (
        <div className="card" key={v.label}>
          <div className="card-head">
            <div className="h-card" style={{ flex: 1 }}>
              Checks · {v.label}
            </div>
            <EvidenceBadge evidence={v.evidence} />
          </div>
          {v.summary ? <p className="ex-verdict-summary">{v.summary}</p> : null}
          <div className="check-list">
            {v.checks.map((c) => (
              <div className="check" key={c.name}>
                <CheckIcon passed={c.passed} />
                <span className="check-name">{c.name.replace(/_/g, " ")}</span>
                <span className="muted">{c.detail}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
