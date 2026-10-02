// A reported experiment (mockup 04): agentd's evidence and summary, the headline
// metrics, the convergence plot, every verdict's checks, variants and provenance.

import type { Experiment, ValidationReport } from "../../api";
import { Icon } from "../../components/Icon";
import { ConvergencePanel } from "./ConvergencePanel";
import { MetricTiles } from "./MetricTiles";
import { Provenance } from "./Provenance";
import { Verdicts } from "./Verdicts";
import { headline, leadVerdict } from "./view";

export function ReportedView({ exp, report }: { exp: Experiment; report: ValidationReport }) {
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
          <div className="ex-banner-summary">{report.summary}</div>
        </div>
      </div>
      <MetricTiles report={report} verdict={verdict} />
      <ConvergencePanel jobs={exp.jobs ?? []} />
      <Verdicts verdicts={report.verdicts} />
      <Provenance report={report} />
    </div>
  );
}
