import type { PollSummary, ResearchItem } from "../../api";
import { useNav } from "../../app/navigation";
import { ErrorNote } from "../../components/ui";

/** What a poll's summary says beyond the counts: agentd's error, why papers were
 *  skipped (verbatim), and which experiments were proposed. */
export function PollDetails({ summary, papers }: { summary: PollSummary; papers: ResearchItem[] }) {
  const nav = useNav();
  const byId = new Map(papers.map((p) => [p.id, p]));
  return (
    <>
      {summary.error ? <ErrorNote>{summary.error}</ErrorNote> : null}
      {summary.skipped.length ? (
        <div>
          <div className="small muted" style={{ marginBottom: 4 }}>
            Skipped
          </div>
          <ul className="tl-list">
            {summary.skipped.map((s, i) => {
              const paper = byId.get(s.item);
              return (
                <li key={`${s.item}-${i}`}>
                  {paper ? (
                    <button className="tl-link" onClick={() => nav.selectPaper(paper.id)}>
                      <b>{paper.data.paper?.title ?? paper.title}</b>
                    </button>
                  ) : (
                    <span className="mono">{s.item}</span>
                  )}
                  : {s.why}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {summary.proposed.length ? (
        <div className="small muted">
          Proposed for approval: <span className="mono">{summary.proposed.join(", ")}</span>
        </div>
      ) : null}
    </>
  );
}
