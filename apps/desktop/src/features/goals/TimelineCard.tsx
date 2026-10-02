// One card of the research timeline. Each kind of entry reads its own real data;
// sentences (triage.why, error, summary) are shown as agentd wrote them.

import type { ReactNode } from "react";
import type { Experiment, Finding, ResearchItem } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { when } from "../../components/time";
import { Chip, EvidenceBadge, StateChip } from "../../components/ui";
import { paperMeta, paperTitle } from "../papers/format";
import { PollDetails } from "./PollDetails";
import { openTarget, pollLine, variantsLine, type OpenTarget, type TimelineEntry } from "./timeline";

type Tone = "yellow" | "blush" | "lavender" | "powder" | "mint" | "gray";

function Shell({
  tone,
  icon,
  title,
  sub,
  ts,
  side,
  children,
}: {
  tone: Tone;
  icon: string;
  title: ReactNode;
  sub?: ReactNode;
  ts: number;
  side?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="tl-item fade-in">
      <span className={`tl-dot ${tone}`} />
      <div className="card tl-card">
        <span className={`icon-tile round ${tone === "yellow" ? "" : tone}`}>
          <Icon name={icon} size={24} />
        </span>
        <div className="tl-body">
          <div className="tl-title">{title}</div>
          {sub ? <div className="tl-sub">{sub}</div> : null}
          {children ? <div className="tl-extra">{children}</div> : null}
        </div>
        <div className="tl-side">
          <span className="tl-time">{when(ts)}</span>
          {side ? (
            <>
              <span className="spacer" />
              {side}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function MiniPaper({ item, chip }: { item: ResearchItem; chip?: ReactNode }) {
  const nav = useNav();
  return (
    <button className="tl-paper" onClick={() => nav.selectPaper(item.id)} title="Open paper">
      <span style={{ minWidth: 0 }}>
        <span className="tl-paper-title" style={{ display: "block" }}>
          {paperTitle(item)}
        </span>
        <span className="tl-paper-meta" style={{ display: "block" }}>
          {paperMeta(item)}
        </span>
        <span className="row" style={{ gap: 8 }}>
          {chip}
          <StateChip kind="paper" state={item.state} />
        </span>
      </span>
    </button>
  );
}

/** "Open": the experiment's paper, or the experiment itself when it has none. */
function OpenButton({ target }: { target: OpenTarget }) {
  const nav = useNav();
  if (!target) return null;
  return (
    <button
      className="btn"
      onClick={() => ("paperId" in target ? nav.selectPaper(target.paperId) : nav.showExperiment(target.experimentId))}
      title={"paperId" in target ? "Open the paper" : "Open the experiment"}
    >
      Open
      <Icon name="chevronRight" size={16} />
    </button>
  );
}

function Claims({ finding }: { finding: Finding }) {
  if (!finding.claims.length) return null;
  return (
    <div className="row" style={{ gap: 8 }}>
      {finding.claims.map((c, i) => (
        <Chip key={`${c.claim}-${i}`} tone={c.holds === true ? "green" : c.holds === false ? "red" : "gray"}>
          <Icon name={c.holds === true ? "check" : c.holds === false ? "x" : "info"} size={14} />
          {c.claim}
          {c.holds === true ? " holds" : c.holds === false ? " did not hold" : " not checked"}
        </Chip>
      ))}
    </div>
  );
}

const RESULT_TITLES: Record<string, string> = {
  reported: "Result reported",
  failed: "Experiment failed",
  rejected: "Experiment rejected",
  cancelled: "Experiment cancelled",
};

function ResultCard({
  ts,
  experiment,
  finding,
}: {
  ts: number;
  experiment: Experiment | null;
  finding: Finding | null;
}) {
  const evidence = finding?.evidence ?? experiment?.evidence ?? null;
  const summary = finding?.summary ?? experiment?.evaluation?.summary ?? experiment?.error ?? null;
  const reported = !experiment || experiment.state === "reported";
  return (
    <Shell
      tone={reported ? "mint" : "gray"}
      icon={reported ? "bars" : "alert"}
      title={experiment ? (RESULT_TITLES[experiment.state] ?? "Experiment finished") : "Finding recorded"}
      sub={experiment ? variantsLine(experiment) : (finding?.scheme_name ?? finding?.experiment_id)}
      ts={ts}
      side={<OpenButton target={openTarget(experiment, finding)} />}
    >
      <div className="row" style={{ gap: 8 }}>
        {reported ? <EvidenceBadge evidence={evidence} /> : null}
        {experiment ? <StateChip kind="experiment" state={experiment.state} /> : null}
      </div>
      {summary ? <div className="tl-text">{summary}</div> : null}
      {finding ? <Claims finding={finding} /> : null}
    </Shell>
  );
}

export function TimelineCard({ entry, papers }: { entry: TimelineEntry; papers: ResearchItem[] }) {
  const nav = useNav();

  switch (entry.kind) {
    case "created":
      return (
        <Shell tone="yellow" icon="sparkle" title="Research started" sub={entry.title ?? undefined} ts={entry.ts} />
      );

    case "poll": {
      const s = entry.summary;
      return (
        <Shell
          tone={s.error ? "blush" : "yellow"}
          icon={s.error ? "alert" : "search"}
          title={s.error ? "Poll couldn't run" : "Poll complete"}
          sub={s.error ? undefined : pollLine(s)}
          ts={entry.ts}
        >
          {s.error || s.skipped.length || s.proposed.length ? <PollDetails summary={s} papers={papers} /> : null}
        </Shell>
      );
    }

    case "paper": {
      const item = entry.item;
      if (entry.phase === "carded") {
        const mapped = Boolean(item.data.scheme_ir);
        return (
          <Shell tone="blush" icon="paper" title="Paper carded" ts={entry.ts}>
            <MiniPaper
              item={item}
              chip={<Chip tone={mapped ? "lavender" : "gray"}>{mapped ? "Scheme mapped" : "No scheme mapped"}</Chip>}
            />
            {!mapped && item.data.scheme_note ? <div className="small muted">{item.data.scheme_note}</div> : null}
            {item.data.proposal_note ? <div className="small muted">{item.data.proposal_note}</div> : null}
          </Shell>
        );
      }
      if (entry.phase === "dismissed") {
        return (
          <Shell tone="gray" icon="paper" title="Paper dismissed" ts={entry.ts}>
            <MiniPaper item={item} />
            {item.data.triage?.why ? <div className="tl-text">{item.data.triage.why}</div> : null}
          </Shell>
        );
      }
      if (entry.phase === "failed") {
        return (
          <Shell tone="blush" icon="alert" title="Couldn't read a paper" ts={entry.ts}>
            <MiniPaper item={item} />
            {item.data.error ? <div className="note error">{item.data.error}</div> : null}
          </Shell>
        );
      }
      return (
        <Shell tone="lavender" icon="paper" title="Reading a paper" ts={entry.ts}>
          <MiniPaper item={item} />
          {item.data.error ? <div className="small muted">{item.data.error}</div> : null}
        </Shell>
      );
    }

    case "experiment": {
      const exp = entry.experiment;
      const approvalId = entry.approvalId;
      return (
        <Shell
          tone="powder"
          icon="flask"
          title="Experiment proposed"
          sub={variantsLine(exp)}
          ts={entry.ts}
          side={
            approvalId ? (
              <button className="btn primary" onClick={() => nav.open({ kind: "review", approvalId })}>
                Review
                <Icon name="chevronRight" size={16} />
              </button>
            ) : (
              <OpenButton target={openTarget(exp)} />
            )
          }
        >
          <div className="row" style={{ gap: 8 }}>
            <StateChip kind="experiment" state={exp.state} />
            {approvalId ? <span className="small muted">Nothing runs until you approve.</span> : null}
          </div>
        </Shell>
      );
    }

    case "result":
      return <ResultCard ts={entry.ts} experiment={entry.experiment} finding={entry.finding} />;

    case "finding":
      return <ResultCard ts={entry.ts} experiment={null} finding={entry.finding} />;
  }
}
