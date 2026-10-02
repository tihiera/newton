import type { Goal, GoalStatus } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ago, when } from "../../components/time";
import { Chip, Note, Spinner, StateChip } from "../../components/ui";
import { StatusMenu } from "./StatusMenu";
import { pollStatus } from "./timeline";

/** Mockup 01's header: title, status, Poll now, New experiment (built-in schemes), the
 *  options menu and the settings; agentd's sentence when the last poll failed. */
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
  const nav = useNav();
  const poll = pollStatus(goal);
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
        <button className="btn" onClick={() => nav.open({ kind: "new-experiment", goalId: goal.id })}>
          <Icon name="flask" size={18} />
          New experiment
        </button>
        <StatusMenu status={goal.status} busy={updating} onChange={onStatus} />
      </div>
      <div className="goal-meta">
        <span>Last looked {ago(goal.last_polled_at)}</span>
        {poll.nextAt ? (
          <>
            <span>·</span>
            <span>Next look: {when(poll.nextAt)}</span>
          </>
        ) : null}
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
      {poll.error ? (
        <div className="goal-poll-error">
          <Note tone="warn" icon="alert">
            {poll.error}
          </Note>
        </div>
      ) : null}
    </div>
  );
}
