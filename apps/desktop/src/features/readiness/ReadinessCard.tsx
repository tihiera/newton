// "What this Mac can do": GET /readiness as a checklist, each item with agentd's
// title, sentence and action. The actions open the drawers or the built-in experiment
// dialog.

import type { ReadinessItem } from "../../api";
import { useReadiness } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner } from "../../components/ui";
import { actionTarget, readinessLine, stateLook } from "./readiness";
import "./readiness.css";

function Row({ item, onAction }: { item: ReadinessItem; onAction: () => void }) {
  const look = stateLook(item.state);
  return (
    <li className={`ready-row ${item.state}`}>
      <span className={`icon-tile round ${look.tile}`}>
        <Icon name={look.icon} size={18} title={look.label} />
      </span>
      <div className="ready-text">
        <div className="ready-title">{item.title}</div>
        <div className="small muted ready-detail">{item.detail}</div>
      </div>
      {item.action ? (
        <button className={`btn ${item.action.kind === "new_experiment" ? "primary" : ""}`} onClick={onAction}>
          {item.action.label}
        </button>
      ) : (
        <span />
      )}
    </li>
  );
}

/** `firstRun`: the welcome card shown while there is no research yet. */
export function ReadinessCard({ firstRun = false, goalId = null }: { firstRun?: boolean; goalId?: string | null }) {
  const nav = useNav();
  const readiness = useReadiness();
  const items = readiness.data?.items ?? [];

  const act = (item: ReadinessItem) => {
    const target = item.action ? actionTarget(item.action.kind, goalId) : null;
    if (target) nav.open(target);
  };

  return (
    <section className={`card ready-card ${firstRun ? "mesh-card first-run" : ""}`}>
      <div className="card-head">
        <span className="icon-tile">
          <Icon name="laptop" />
        </span>
        <div className="spacer">
          <h2 className={firstRun ? "h-section" : "h-card"}>
            {firstRun ? "Welcome to Newton" : "What this Mac can do"}
          </h2>
          <div className="small muted">
            {firstRun ? "Here is what's ready on this Mac." : items.length ? readinessLine(items) : "Checking…"}
          </div>
        </div>
        {firstRun ? null : (
          <button className="btn" onClick={() => nav.open({ kind: "new-experiment", goalId })}>
            <Icon name="flask" size={16} />
            New experiment
          </button>
        )}
      </div>
      {readiness.error && !readiness.data ? <ErrorNote error={readiness.error} /> : null}
      {!readiness.data && !readiness.error ? (
        <div className="row muted">
          <Spinner /> Checking…
        </div>
      ) : null}
      {items.length ? (
        <ul className="ready-list">
          {items.map((it) => (
            <Row key={it.key} item={it} onAction={() => act(it)} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}
