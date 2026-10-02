import { useMemo } from "react";
import { api, type ResearchItem } from "../../api";
import { useExperiments, useGoals } from "../../app/data";
import { useNav } from "../../app/navigation";
import { saveFile } from "../../app/platform";
import { useAction } from "../../hooks/useAction";
import { ExternalLink } from "../../components/ExternalLink";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, Spinner, StateChip } from "../../components/ui";
import { experimentsFor } from "../experiments/view";
import { canReadAgain, exportFileName, latestReported, paperSubline, paperTitle, paperUrl } from "./format";

/** Mockup 02's header: back link, big title, sub line, state and claim chips; the
 *  export of the newest reported experiment (mockup 06) and the link to arXiv. */
export function PaperHeader({ item }: { item: ResearchItem }) {
  const nav = useNav();
  const goals = useGoals();
  const goal = nav.goalId ? goals.data?.find((g) => g.id === nav.goalId) : undefined;
  const method = item.data.card?.method;
  const url = paperUrl(item);
  // agentd retries a failed paper (or one whose triage failed) when it is ingested
  // again (same item, fresh read).
  const retry = useAction(() => api.research.ingest({ ref: item.external_id, goal_id: item.goal_id }));
  const experiments = useExperiments();
  const reported = useMemo(
    () => latestReported(experimentsFor(experiments.data ?? [], item.id)),
    [experiments.data, item.id],
  );
  // The user picks where the zip goes in the shell's save dialog (null: cancelled).
  const exporter = useAction(async (id: string, name: string) => saveFile(name, await api.experiments.exportZip(id)));

  return (
    <div className="mesh-header pw-head">
      <button className="pw-back" onClick={() => nav.selectPaper(null)}>
        <Icon name="chevronLeft" size={15} />
        {nav.goalId ? (goal?.title ?? "Research") : "Papers"}
      </button>
      <h1 className="pw-title">{paperTitle(item)}</h1>
      <p className="pw-sub">{paperSubline(item)}</p>
      <div className="pw-chips">
        <StateChip kind="paper" state={item.state} large />
        {method?.tvd ? (
          <Chip tone="lavender" large>
            <Icon name="sparkle" size={16} />
            TVD claimed
          </Chip>
        ) : null}
        {typeof method?.order === "number" ? (
          <Chip tone="outline" large>
            Order {method.order} claimed
          </Chip>
        ) : null}
        <span className="spacer" />
        {canReadAgain(item) ? (
          <button className="btn" disabled={retry.busy} onClick={() => void retry.run()}>
            {retry.busy ? <Spinner /> : <Icon name="refresh" size={16} />}
            Read again
          </button>
        ) : null}
        {reported ? (
          <button
            className="btn"
            disabled={exporter.busy}
            onClick={() => void exporter.run(reported.id, exportFileName(reported))}
            title="Save the newest report, its data and figures as a zip"
          >
            {exporter.busy ? <Spinner /> : <Icon name="share" size={16} />}
            Export
          </button>
        ) : null}
        {url ? (
          <ExternalLink className="icon-btn outlined" href={url} aria-label="Open on arXiv" title="Open on arXiv">
            <Icon name="link" size={18} />
          </ExternalLink>
        ) : null}
      </div>
      {retry.error ? <ErrorNote error={retry.error} /> : null}
      {exporter.error ? <ErrorNote error={exporter.error} /> : null}
    </div>
  );
}
