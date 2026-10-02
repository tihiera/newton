// One experiment on its own in the workspace: one made from built-in schemes (no
// paper), or one opened from an approval or the research timeline. A paper's
// experiment also links back to its paper.

import { useNav } from "../../app/navigation";
import { useExperiments, useGoals } from "../../app/data";
import { Icon } from "../../components/Icon";
import { ExperimentDetail } from "./ExperimentDetail";
import "./experiments.css";

export function ExperimentWorkspace({ experimentId }: { experimentId: string }) {
  const nav = useNav();
  const goals = useGoals();
  const experiments = useExperiments();
  const exp = experiments.data?.find((e) => e.id === experimentId);
  const goal = nav.goalId ? goals.data?.find((g) => g.id === nav.goalId) : undefined;
  const paperId = exp?.research_item_id ?? null;
  const goalId = exp ? exp.goal_id : nav.goalId;

  // Under a finished experiment without a paper: another one from the library.
  const next = paperId ? null : (
    <div>
      <button className="btn ex-new" onClick={() => nav.open({ kind: "new-experiment", goalId })}>
        <Icon name="flask" size={18} />
        New experiment
      </button>
    </div>
  );

  return (
    <div className="workspace-scroll">
      <div className="mesh-header pw-head ex-workspace-head">
        <button className="pw-back" onClick={() => nav.showExperiment(null)}>
          <Icon name="chevronLeft" size={15} />
          {nav.goalId ? (goal?.title ?? "Research") : "Papers"}
        </button>
        <div className="pw-chips" style={{ marginTop: 0 }}>
          <span className="muted">
            {exp ? (paperId ? "Experiment from a paper" : "Experiment from built-in schemes") : "Experiment"}
          </span>
          {paperId ? (
            <button className="btn" onClick={() => nav.selectPaper(paperId)}>
              <Icon name="paper" size={16} />
              Open paper
            </button>
          ) : null}
        </div>
      </div>
      <div style={{ marginTop: 22 }}>
        <ExperimentDetail key={experimentId} experimentId={experimentId} next={next} />
      </div>
    </div>
  );
}
