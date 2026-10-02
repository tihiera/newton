// One experiment, read from agentd every few seconds until it is finished:
// Plan -> Run -> Evaluate -> Reported, its jobs and logs, and the evidence.
// `next` is what the paper offers next: under a reported experiment's evidence, or under
// an experiment that ended without a report.

import { useCallback, useState, type ReactNode } from "react";
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

export function ExperimentDetail({ experimentId, next }: { experimentId: string; next?: ReactNode }) {
  // Live (re-read every few seconds) until the experiment is over; then it only re-reads
  // on events. Switched while rendering, when the state it follows changes.
  const [live, setLive] = useState(true);
  const q = usePolling((s) => api.experiments.get(experimentId, s), [experimentId], {
    interval: live ? LIVE_MS : 0,
  });
  const exp = q.data;
  const terminal = exp ? isTerminal(exp.state) : false;
  if (live === terminal) setLive(!terminal);

  if (!exp) {
    return q.error ? (
      <ErrorNote error={q.error} />
    ) : (
      <div className="row muted">
        <Spinner /> Loading the experiment…
      </div>
    );
  }
  return <ExperimentBody exp={exp} refresh={q.refresh} next={next} />;
}

function ExperimentBody({ exp, refresh, next }: { exp: Experiment; refresh: () => void; next?: ReactNode }) {
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
      {reported && exp.evaluation ? <ReportedView exp={exp} report={exp.evaluation} next={next} /> : null}
      {/* Over without a report (rejected, cancelled, failed): the paper's next step goes
          here, where the experiment is shown (agentd lets the paper propose again). */}
      {isTerminal(exp.state) && !(reported && exp.evaluation) ? next : null}

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

      {overlay === "report" ? (
        <ReportModal experimentId={exp.id} title={exp.title} onClose={() => setOverlay(null)} />
      ) : null}
      {overlay === "publish" ? <PublishDialog experiment={exp} onClose={() => setOverlay(null)} /> : null}
    </div>
  );
}
