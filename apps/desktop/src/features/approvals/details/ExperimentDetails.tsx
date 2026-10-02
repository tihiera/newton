// execute_experiment details (mockup 03): placement, estimate, variants, hypothesis,
// timeout. Values verbatim from agentd's approval.

import { duration, num } from "../../../components/time";
import { numOrUndef, obj, shortHash, str, variantScheme } from "../text";
import { DetailRow, Pairs } from "./DetailRow";

const ROLE: Record<string, string> = { baseline: "Baseline", candidate: "Candidate" };

export function ExperimentDetails({ details }: { details: Record<string, unknown> }) {
  const host = obj(details.host);
  const backend = str(details.backend);
  const where = [str(host?.name), str(details.device), backend?.toUpperCase()].filter(Boolean).join(", ");
  const seconds = numOrUndef(details.estimated_seconds);
  const peak = numOrUndef(details.estimated_peak_gb);
  const variants = Array.isArray(details.variants) ? (details.variants as Array<Record<string, unknown>>) : [];
  const hypothesis = str(details.hypothesis);
  const timeout = numOrUndef(details.timeout_seconds);
  const benchmark = str(details.benchmark);
  const objective = str(details.objective);

  return (
    <div>
      <DetailRow icon="chip" title="Placement">
        <div className="ap-strong">{where || "—"}</div>
        {str(details.placement) ? <div>Reason: {str(details.placement)}</div> : null}
      </DetailRow>
      <DetailRow icon="clock" tile="blush" title="Estimate">
        <div className="ap-strong">
          {duration(seconds)}
          <span className="ap-sep">·</span>
          peak memory {peak !== undefined ? `${num(peak)} GB` : "—"}
        </div>
        {str(details.estimate_basis) ? <div className="small">{str(details.estimate_basis)}</div> : null}
      </DetailRow>
      <DetailRow icon="paper" tile="lavender" title="Variants">
        {variants.length ? (
          <Pairs
            rows={variants.map((v) => {
              const s = variantScheme(v);
              const role = ROLE[String(v.role)] ?? String(v.role ?? "Variant");
              return [
                `${role}:`,
                <>
                  {String(v.label ?? "—")}
                  <span className="ap-sep">·</span>
                  <span className="mono">{s.scheme}</span>
                  {s.digest ? <span className="muted small"> (IR {shortHash(s.digest, 6, 4)})</span> : null}
                </>,
              ];
            })}
          />
        ) : (
          "—"
        )}
      </DetailRow>
      <DetailRow icon="target" tile="powder" title="Hypothesis">
        <div className="ap-strong">{hypothesis ?? "None given"}</div>
        {benchmark || objective ? (
          <div className="small">
            {[benchmark, objective ? `objective: ${objective}` : null].filter(Boolean).join(" · ")}
          </div>
        ) : null}
      </DetailRow>
      <DetailRow icon="stop" tile="mint" title="Timeout">
        <div className="ap-strong">{timeout !== undefined ? duration(timeout) : "—"}</div>
      </DetailRow>
    </div>
  );
}
