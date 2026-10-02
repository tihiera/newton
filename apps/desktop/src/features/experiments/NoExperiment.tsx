// A paper without an experiment: the propose card when the card mapped onto
// Newton's IR and the paper is carded; otherwise agentd's own reason.

import type { ResearchItem } from "../../api";
import { Icon } from "../../components/Icon";
import { Note, StateChip } from "../../components/ui";
import { ProposeCard } from "./ProposeCard";

export function NoExperiment({ item }: { item: ResearchItem }) {
  const d = item.data;
  if (d.scheme_ir && item.state === "carded") return <ProposeCard item={item} />;

  return (
    <div className="card soft ex-none">
      <div className="card-head">
        <span className="icon-tile powder">
          <Icon name="flask" size={22} />
        </span>
        <div style={{ flex: 1 }}>
          <div className="h-card">No experiment for this paper</div>
          <div className="row small muted" style={{ marginTop: 4 }}>
            Paper state <StateChip kind="paper" state={item.state} />
          </div>
        </div>
      </div>
      <div className="stack" style={{ gap: 10 }}>
        {!d.scheme_ir && d.scheme_note ? <Note icon="graph">{d.scheme_note}</Note> : null}
        {!d.scheme_ir && !d.scheme_note ? (
          <Note>This paper has no scheme mapped onto Newton's IR yet, so there is nothing to test.</Note>
        ) : null}
        {d.proposal_note ? <Note>{d.proposal_note}</Note> : null}
        {d.error ? <Note tone="error" icon="alert">{d.error}</Note> : null}
      </div>
    </div>
  );
}
