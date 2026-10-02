// The library when no paper is selected: what Newton has read, by state, the most
// recent cards, experiments made from built-in schemes, and what this Mac can do
// (agentd's readiness checklist; the welcome card while there is no research yet).

import { useMemo } from "react";
import type { Experiment } from "../../api";
import { useExperiments, useGoals, usePapers, useProfile } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { stateLabel } from "../../components/labels";
import { when } from "../../components/time";
import { Empty, ErrorNote, EvidenceBadge, Note, Spinner, StateChip } from "../../components/ui";
import { paperlessExperiments } from "../experiments/library";
import { variantsLine } from "../goals/timeline";
import { ReadinessCard } from "../readiness/ReadinessCard";
import { isFirstRun } from "../readiness/readiness";
import { countByState, paperMeta, paperTitle } from "./format";
import "../experiments/experiments.css";
import "./papers.css";

const RECENT = 8;
const PAPERLESS = 6;

function PaperlessExperiments({ list }: { list: Experiment[] }) {
  const nav = useNav();
  if (!list.length) return null;
  return (
    <div>
      <h2 className="h-section" style={{ marginBottom: 14 }}>
        Experiments from built-in schemes
      </h2>
      <div className="paperless-list">
        {list.map((e) => (
          <button key={e.id} className="card paperless-card" onClick={() => nav.showExperiment(e.id)}>
            <div className="paper-row-title">{e.title}</div>
            <div className="paper-row-meta">{variantsLine(e)}</div>
            <div className="row" style={{ gap: 8 }}>
              <StateChip kind="experiment" state={e.state} />
              {e.state === "reported" ? <EvidenceBadge evidence={e.evidence} /> : null}
              <span className="small muted">{when(e.created_at)}</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

export function PapersHome() {
  const nav = useNav();
  const papers = usePapers(null);
  const profile = useProfile();
  const goals = useGoals();
  const experiments = useExperiments();
  const items = papers.data ?? [];
  const recent = [...items].sort((a, b) => b.updated_at - a.updated_at).slice(0, RECENT);
  const paperless = useMemo(() => paperlessExperiments(experiments.data ?? []).slice(0, PAPERLESS), [experiments.data]);
  const firstRun = isFirstRun(goals.data, papers.data);
  const addPaper = () => nav.open({ kind: "ingest", goalId: null });
  const newExperiment = () => nav.open({ kind: "new-experiment", goalId: null });

  return (
    <div className="workspace-scroll">
      <div className="mesh-header pw-head">
        <h1 className="pw-title">Papers</h1>
        <p className="pw-sub">Every paper Newton has read, across all your research.</p>
        <div className="pw-chips">
          <button className="btn primary" onClick={addPaper}>
            <Icon name="plus" size={16} />
            Add arXiv paper
          </button>
          <button className="btn" onClick={newExperiment}>
            <Icon name="flask" size={16} />
            New experiment from built-in schemes
          </button>
        </div>
      </div>

      <div className="stack" style={{ gap: 26, marginTop: 30 }}>
        {firstRun ? <ReadinessCard firstRun /> : null}
        {!firstRun && profile.data && !profile.data.default_model ? (
          <Note tone="warn">
            No default model is set, so Newton can't read papers yet.{" "}
            <button className="btn ghost" style={{ padding: "2px 8px" }} onClick={() => nav.open({ kind: "settings" })}>
              Open Settings
            </button>
          </Note>
        ) : null}

        {papers.error && !papers.data ? <ErrorNote error={papers.error} /> : null}

        {papers.data === undefined && papers.loading ? (
          <div className="empty">
            <Spinner size={22} />
          </div>
        ) : papers.data && items.length === 0 && !firstRun ? (
          <Empty title="No papers yet" icon="paper">
            Paste an arXiv link and Newton turns the paper into a research card: summary, method, claims, and whether it
            maps onto a scheme you can test.
            <div style={{ marginTop: 16 }}>
              <button className="btn primary" onClick={addPaper}>
                <Icon name="plus" size={16} />
                Add arXiv paper
              </button>
            </div>
          </Empty>
        ) : items.length ? (
          <>
            <div className="grid-tiles">
              <div className="card soft metric">
                <div className="metric-label">
                  <Icon name="paper" size={17} />
                  All papers
                </div>
                <div className="metric-value">{items.length}</div>
              </div>
              {countByState(items).map(([state, n]) => (
                <div className="card soft metric" key={state}>
                  <div className="metric-label">
                    <StateChip kind="paper" state={state} />
                  </div>
                  <div className="metric-value" aria-label={`${stateLabel("paper", state)[0]}: ${n}`}>
                    {n}
                  </div>
                </div>
              ))}
            </div>

            <div>
              <h2 className="h-section" style={{ marginBottom: 14 }}>
                Recent
              </h2>
              <div className="home-list">
                {recent.map((it) => (
                  <button key={it.id} className="card home-card" onClick={() => nav.selectPaper(it.id)}>
                    <div style={{ minWidth: 0 }}>
                      <div className="paper-row-title">{paperTitle(it)}</div>
                      <div className="paper-row-meta">{paperMeta(it)}</div>
                      <StateChip kind="paper" state={it.state} />
                      {it.data.card?.summary ? <div className="home-card-summary">{it.data.card.summary}</div> : null}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          </>
        ) : null}

        <PaperlessExperiments list={paperless} />
        {firstRun ? null : <ReadinessCard />}
      </div>
    </div>
  );
}
