// One research goal (mockup 01): its header and the research timeline, built from
// the goal's events, papers, experiments, pending approvals and findings.

import { useContext, useMemo, useState } from "react";
import { api, type GoalStatus, type PollSummary } from "../../api";
import { useApprovals, useExperiments, useGoals, usePapers } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { Empty, ErrorNote, Note, Spinner } from "../../components/ui";
import { EventsContext } from "../../hooks/revision";
import { useAction } from "../../hooks/useAction";
import { usePolling } from "../../hooks/usePolling";
import { GoalHeader } from "./GoalHeader";
import { PollDetails } from "./PollDetails";
import { buildTimeline, pollLine } from "./timeline";
import { TimelineCard } from "./TimelineCard";
import "./goals.css";

const pollGoal = (id: string) => api.goals.poll(id);
const setStatus = (id: string, status: GoalStatus) => api.goals.update(id, { status });

export function GoalWorkspace({ goalId }: { goalId: string }) {
  const nav = useNav();
  const goals = useGoals();
  const papers = usePapers(goalId);
  const experiments = useExperiments();
  const approvals = useApprovals();
  const findings = usePolling((s) => api.research.findings(goalId, s), [goalId], { interval: 10000 });
  // The goal's whole event history once; newer events arrive through EventsContext.
  const history = usePolling(
    (s) => api.events(0, { entity_type: "goal", entity_id: goalId, limit: 200 }, s),
    [goalId],
    { interval: 0, followEvents: false },
  );
  const { recent } = useContext(EventsContext);
  const poll = useAction(pollGoal);
  const update = useAction(setStatus);
  const [lastPoll, setLastPoll] = useState<PollSummary | null>(null);

  const goal = goals.data?.find((g) => g.id === goalId);
  const items = papers.data ?? [];

  const entries = useMemo(
    () =>
      buildTimeline({
        goalId,
        events: [...(history.data ?? []), ...recent],
        papers: papers.data,
        experiments: experiments.data,
        approvals: approvals.data,
        findings: findings.data,
      }),
    [goalId, history.data, recent, papers.data, experiments.data, approvals.data, findings.data],
  );

  if (!goal) {
    return (
      <div className="workspace-scroll">
        {goals.data ? (
          <Empty title="This research isn't there any more" icon="folder">
            <button className="btn" onClick={nav.showPapers}>
              Back to papers
            </button>
          </Empty>
        ) : goals.error ? (
          <ErrorNote error={goals.error} />
        ) : (
          <div className="empty">
            <Spinner size={22} />
          </div>
        )}
      </div>
    );
  }

  const runPoll = async () => {
    setLastPoll(null);
    const summary = await poll.run(goalId);
    if (summary) setLastPoll(summary);
    papers.refresh();
    goals.refresh();
    findings.refresh();
  };
  const changeStatus = async (status: GoalStatus) => {
    if (await update.run(goalId, status)) goals.refresh();
  };
  const loadingTimeline = history.data === undefined && papers.data === undefined;

  return (
    <div className="workspace-scroll">
      <GoalHeader goal={goal} polling={poll.busy} updating={update.busy} onPoll={runPoll} onStatus={changeStatus} />

      <div className="stack poll-result">
        {poll.busy ? (
          <Note icon="search">
            Looking for new papers on arXiv. Reading each new one with the model can take a while.
          </Note>
        ) : null}
        {poll.error ? <ErrorNote error={poll.error} /> : null}
        {update.error ? <ErrorNote error={update.error} /> : null}
        {lastPoll ? (
          <section className="card mesh-card">
            <div className="card-head">
              <span className="icon-tile">
                <Icon name="search" />
              </span>
              <div className="spacer">
                <div className="h-card">{lastPoll.error ? "Poll couldn't run" : "Poll complete"}</div>
                {!lastPoll.error ? <div className="muted">{pollLine(lastPoll)}</div> : null}
              </div>
              <button className="icon-btn" onClick={() => setLastPoll(null)} aria-label="Dismiss">
                <Icon name="x" size={18} />
              </button>
            </div>
            <div className="stack" style={{ gap: 10 }}>
              <PollDetails summary={lastPoll} papers={items} />
            </div>
          </section>
        ) : null}
      </div>

      <h2 className="h-section goal-section-title" style={{ fontSize: 28 }}>
        Research timeline
      </h2>
      {loadingTimeline ? (
        <div className="empty">
          <Spinner size={22} />
        </div>
      ) : entries.length === 0 ? (
        <Empty title="Nothing yet" icon="search">
          Press Poll now to look for papers on arXiv, or add a paper you already know.
          <div style={{ marginTop: 16 }}>
            <button className="btn" onClick={() => nav.open({ kind: "ingest", goalId })}>
              <Icon name="plus" size={16} />
              Add arXiv paper
            </button>
          </div>
        </Empty>
      ) : (
        <div className="timeline">
          {entries.map((e) => (
            <TimelineCard key={e.key} entry={e} papers={items} />
          ))}
        </div>
      )}
    </div>
  );
}
