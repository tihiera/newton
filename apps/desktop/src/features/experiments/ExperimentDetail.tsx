// One experiment, read from agentd every few seconds until it is finished:
// Plan -> Run -> Evaluate -> Reported, its jobs and logs, and the evidence.

import { useCallback, useEffect, useState } from "react";
import { api, type Experiment } from "../../api";
import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner, StateChip, Stepper } from "../../components/ui";
import { when } from "../../components/time";
import { useAction } from "../../hooks/useAction";
import { usePolling } from "../../hooks/usePolling";
import { PublicationsList } from "../publishing/PublicationsList";
import { PublishDialog } from "../publishing/PublishDialog";
import { AwaitingCard } from "./AwaitingCard";
import { JobsList } from "./JobsList";
import { ReportModal } from "./ReportModal";
import { ReportedView } from "./ReportedView";
import { isTerminal, STEPS, stepOf } from "./view";

const LIVE_MS = 3000;

export function ExperimentDetail({ experimentId }: { experimentId: string }) {
  const [interval, setIntervalMs] = useState(LIVE_MS);
  const q = usePolling((s) => api.experiments.get(experimentId, s), [experimentId], { interval });
  const exp = q.data;
  const terminal = exp ? isTerminal(exp.state) : false;
  useEffect(() => setIntervalMs(terminal ? 0 : LIVE_MS), [terminal]);

  if (!exp) {
    return q.error ? (
      <ErrorNote error={q.error} />
    ) : (
      <div className="row muted">
        <Spinner /> Loading the experiment…
      </div>
    );
  }
  return <ExperimentBody exp={exp} refresh={q.refresh} />;
}

function ExperimentBody({ exp, refresh }: { exp: Experiment; refresh: () => void }) {
  const [overlay, setOverlay] = useState<"report" | "publish" | null>(null);
  const cancel = useAction(useCallback(() => api.experiments.cancel(exp.id), [exp.id]));
  const step = stepOf(exp);
  const cancellable = exp.state === "awaiting_approval" || exp.state === "executing";
  const reported = exp.state === "reported";

  return (
    <div className="stack ex-detail">
      <div className="ex-head">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-section">{exp.title}</div>
          <div className="row small muted" style={{ marginTop: 6 }}>
            <StateChip kind="experiment" state={exp.state} />
            <span>{exp.spec?.benchmark}</span>
            <span>·</span>
            <span>Created {when(exp.created_at)}</span>
          </div>
        </div>
        <div className="top-actions">
          {exp.report_path ? (
            <button className="btn" onClick={() => setOverlay("report")}>
              <Icon name="paper" size={18} />
              View report
            </button>
          ) : null}
          {reported ? (
            <button className="btn primary" onClick={() => setOverlay("publish")}>
              <Icon name="share" size={18} />
              Publish
            </button>
          ) : null}
        </div>
      </div>

      <div className="ex-stepper">
        <Stepper steps={STEPS} current={step.current} failed={step.failed} />
      </div>

      {exp.error ? (
        <ErrorNote>
          <strong>{exp.state === "failed" ? "Failed: " : ""}</strong>
          {exp.error}
        </ErrorNote>
      ) : null}

      {exp.state === "awaiting_approval" ? <AwaitingCard exp={exp} /> : null}
      {reported && exp.evaluation ? <ReportedView exp={exp} report={exp.evaluation} /> : null}

      {exp.jobs?.length ? <JobsList jobs={exp.jobs} live={!isTerminal(exp.state)} /> : null}

      {cancellable ? (
        <div className="row">
          <button
            className="btn danger"
            disabled={cancel.busy}
            onClick={async () => {
              if (await cancel.run()) refresh();
            }}
          >
            {cancel.busy ? <Spinner /> : <Icon name="stop" size={16} />}
            Cancel experiment
          </button>
          {cancel.error ? <ErrorNote error={cancel.error} /> : null}
        </div>
      ) : null}

      {reported ? <PublicationsList experimentId={exp.id} /> : null}

      {overlay === "report" ? <ReportModal experimentId={exp.id} title={exp.title} onClose={() => setOverlay(null)} /> : null}
      {overlay === "publish" ? <PublishDialog experiment={exp} onClose={() => setOverlay(null)} /> : null}
    </div>
  );
}
