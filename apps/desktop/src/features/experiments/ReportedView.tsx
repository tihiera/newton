// A reported experiment (mockup 04): agentd's evidence and summary, the headline
// metrics, the convergence plot, every verdict's checks, variants and provenance.
// `next` sits right under the evidence: the paper's "New experiment".

import type { ReactNode } from "react";
import type { Experiment, ValidationReport } from "../../api";
import { Icon } from "../../components/Icon";
import { ConvergencePanel } from "./ConvergencePanel";
import { MetricTiles } from "./MetricTiles";
import { Provenance } from "./Provenance";
import { Verdicts } from "./Verdicts";
import { headline, leadVerdict, plainResult } from "./view";

export function ReportedView({ exp, report, next }: { exp: Experiment; report: ValidationReport; next?: ReactNode }) {
  const evidence = report.evidence ?? exp.evidence ?? "unknown";
  const verdict = leadVerdict(report);
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className={`banner ${evidence}`}>
        <span className={`icon-tile round ex-banner-icon ev-${evidence}`}>
          <Icon name="bars" size={28} />
        </span>
        <div style={{ minWidth: 0 }}>
          <div className="banner-title">{headline(evidence, verdict)}</div>
          <div className="ex-banner-summary">{plainResult(report, verdict) || report.summary}</div>
        </div>
      </div>
      <MetricTiles report={report} verdict={verdict} />
      <ConvergencePanel jobs={exp.jobs ?? []} />
      {next}
      <Verdicts verdicts={report.verdicts} />
      <Provenance report={report} />
    </div>
  );
}
