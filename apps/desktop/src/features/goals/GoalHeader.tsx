import type { Goal, GoalStatus } from "../../api";
import { Icon } from "../../components/Icon";
import { ago } from "../../components/time";
import { Chip, Spinner, StateChip } from "../../components/ui";
import { StatusMenu } from "./StatusMenu";

/** Mockup 01's header: title, status, Poll now, the options menu and the settings. */
export function GoalHeader({
  goal,
  polling,
  updating,
  onPoll,
  onStatus,
}: {
  goal: Goal;
  polling: boolean;
  updating: boolean;
  onPoll: () => void;
  onStatus: (status: GoalStatus) => void;
}) {
  return (
    <div className="mesh-header goal-head">
      <h1 className="goal-title">{goal.title}</h1>
      {goal.description ? <p className="goal-desc">{goal.description}</p> : null}
      <div className="goal-actions">
        <StateChip kind="goal" state={goal.status} large />
        <button className="btn" onClick={onPoll} disabled={polling}>
          {polling ? <Spinner size={18} /> : <Icon name="bars" size={18} />}
          {polling ? "Polling…" : "Poll now"}
        </button>
        <StatusMenu status={goal.status} busy={updating} onChange={onStatus} />
      </div>
      <div className="goal-meta">
        <span>Last looked {ago(goal.last_polled_at)}</span>
        <span>·</span>
        <span>every {goal.poll_hours} h</span>
        <span>·</span>
        <span>auto-propose {goal.auto_propose ? "on" : "off"}</span>
        {goal.keywords.length ? <span>·</span> : null}
        {goal.keywords.map((k) => (
          <Chip key={k} tone="outline">
            {k}
          </Chip>
        ))}
        {goal.categories.length ? (
          <>
            <span>·</span>
            <span className="mono">{goal.categories.join("  ")}</span>
          </>
        ) : null}
      </div>
    </div>
  );
}
