import { api, type ResearchItem } from "../../api";
import { useGoals } from "../../app/data";
import { useNav } from "../../app/navigation";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, Spinner, StateChip } from "../../components/ui";
import { paperSubline, paperTitle } from "./format";

/** Mockup 02's header: back link, big title, sub line, state and claim chips. */
export function PaperHeader({ item }: { item: ResearchItem }) {
  const nav = useNav();
  const goals = useGoals();
  const goal = nav.goalId ? goals.data?.find((g) => g.id === nav.goalId) : undefined;
  const method = item.data.card?.method;
  const url = item.data.paper?.url || `https://arxiv.org/abs/${item.external_id}`;
  // agentd retries a failed paper when it is ingested again (same item, fresh read).
  const retry = useAction(() => api.research.ingest({ ref: item.external_id, goal_id: item.goal_id }));

  return (
    <div className="mesh-header pw-head">
      <button className="pw-back" onClick={() => nav.selectPaper(null)}>
        <Icon name="chevronLeft" size={15} />
        {nav.goalId ? goal?.title ?? "Research" : "Papers"}
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
        {item.state === "failed" ? (
          <button className="btn" disabled={retry.busy} onClick={() => void retry.run()}>
            {retry.busy ? <Spinner /> : <Icon name="refresh" size={16} />}
            Read again
          </button>
        ) : null}
        <a
          className="icon-btn outlined"
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          aria-label="Open on arXiv"
          title="Open on arXiv"
        >
          <Icon name="share" size={18} />
        </a>
      </div>
      {retry.error ? <ErrorNote error={retry.error} /> : null}
    </div>
  );
}
