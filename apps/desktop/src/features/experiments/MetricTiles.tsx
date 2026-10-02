// Headline numbers, as evaluation.variants reports them: observed order and L2 error
// for candidate vs baseline, then the candidate's claims (holds true/false/null).

import type { CandidateVerdict, ValidationReport } from "../../api";
import { Icon } from "../../components/Icon";
import { num } from "../../components/time";
import { baselineOf, candidateOf, claimLabel, claimOutcome } from "./view";

function Pair({ a, b, aLabel, bLabel }: { a: unknown; b: unknown; aLabel: string; bLabel: string }) {
  return (
    <div className="ex-pair">
      <div>
        <div className="metric-value">{num(a)}</div>
        <div className="metric-sub" title={aLabel}>
          {aLabel}
        </div>
      </div>
      <div className="ex-pair-rule" />
      <div>
        <div className="metric-value">{num(b)}</div>
        <div className="metric-sub" title={bLabel}>
          {bLabel}
        </div>
      </div>
    </div>
  );
}

function short(v: unknown): string {
  if (typeof v === "number") return num(v);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (v === null || v === undefined) return "—";
  return typeof v === "string" ? v : "";
}

/** "claimed 2 · measured 2"; a structured measurement (a CFL sweep, say) is left to
 *  the checks list below, which has agentd's sentence for it. */
function claimLine(claimed: unknown, measured: unknown): string {
  const m = short(measured);
  return m ? `claimed ${short(claimed)} · measured ${m}` : `claimed ${short(claimed)} · see checks`;
}

export function MetricTiles({ report, verdict }: { report: ValidationReport; verdict?: CandidateVerdict }) {
  const cand = candidateOf(report, verdict);
  const base = baselineOf(report);
  const cLabel = cand ? `Candidate · ${cand.label}` : "Candidate";
  const bLabel = base ? `Baseline · ${base.label}` : "Baseline";
  const hasOrder = cand?.metrics.observed_order !== undefined || base?.metrics.observed_order !== undefined;
  const hasL2 = cand?.metrics.l2_error !== undefined || base?.metrics.l2_error !== undefined;

  return (
    <div className="grid-tiles ex-tiles">
      {hasOrder ? (
        <div className="card metric ex-tile-wide">
          <div className="metric-label">
            <span className="ex-mini-icon powder">
              <Icon name="wave" size={16} />
            </span>
            Observed order
          </div>
          <Pair a={cand?.metrics.observed_order} b={base?.metrics.observed_order} aLabel={cLabel} bLabel={bLabel} />
        </div>
      ) : null}
      {hasL2 ? (
        <div className="card metric ex-tile-wide">
          <div className="metric-label">
            <span className="ex-mini-icon powder">
              <Icon name="database" size={16} />
            </span>
            L2 error
          </div>
          <Pair a={cand?.metrics.l2_error} b={base?.metrics.l2_error} aLabel={cLabel} bLabel={bLabel} />
        </div>
      ) : null}
      {(cand?.assumptions ?? []).map((a) => {
        const o = claimOutcome(a);
        return (
          <div className="card metric" key={a.claim}>
            <div className="metric-label">
              <span className={`ex-mini-icon ${o.tone}`}>
                <Icon name={o.icon} size={16} />
              </span>
              {claimLabel(a.claim)}
            </div>
            <div className={`metric-value ev-${o.tone}`}>{o.word}</div>
            <div className="metric-sub">{claimLine(a.claimed, a.measured)}</div>
          </div>
        );
      })}
    </div>
  );
}
