// The paper's other experiments, newest first, compact: title, state, evidence and
// when it was created. Picking one only changes which experiment the tab shows.

import type { Experiment } from "../../api";
import { EvidenceBadge, StateChip } from "../../components/ui";
import { when } from "../../components/time";

export function EarlierExperiments({
  experiments,
  shownId,
  onShow,
}: {
  experiments: Experiment[];
  shownId: string;
  onShow: (id: string) => void;
}) {
  if (!experiments.length) return null;
  return (
    <div className="card ex-earlier">
      <div className="small muted ex-earlier-head">Earlier experiments</div>
      <div className="ex-earlier-list">
        {experiments.map((e) => (
          <button
            key={e.id}
            className={`ex-earlier-row ${e.id === shownId ? "selected" : ""}`}
            aria-current={e.id === shownId ? "true" : undefined}
            onClick={() => onShow(e.id)}
          >
            <span className="ex-earlier-title">{e.title}</span>
            <StateChip kind="experiment" state={e.state} />
            {e.evidence ? <EvidenceBadge evidence={e.evidence} /> : null}
            <span className="small muted ex-earlier-time">{when(e.created_at)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
