import type { ResearchItem } from "../../api";
import { Icon } from "../../components/Icon";
import { stateLabel } from "../../components/labels";
import { Chip, ErrorNote, Note, Spinner } from "../../components/ui";
import { canReadAgain, READING_STATES } from "./format";

/** The card summary, triage, reading state and abstract (title, authors and source are
 *  in the header). */
export function SummaryTab({ item }: { item: ResearchItem }) {
  const { card, paper, triage } = item.data;
  // A paper whose triage failed stays "discovered" with agentd's error: not reading.
  const stuck = item.state === "failed" || canReadAgain(item);
  const reading = READING_STATES.has(item.state) && !stuck;

  return (
    <div className="pw-grid">
      <div className="pw-col">
        {reading ? (
          <div className="note">
            <Spinner size={18} />
            <div>
              <b>{stateLabel("paper", item.state)[0]}.</b>{" "}
              {item.state === "extracting"
                ? "The model is reading the paper and writing its card."
                : "Newton is fetching the paper."}
              {item.data.error ? <div style={{ marginTop: 6 }}>{item.data.error}</div> : null}
            </div>
          </div>
        ) : null}
        {stuck ? (
          <ErrorNote>
            <b>Couldn't read this paper.</b> {item.data.error ?? ""}
          </ErrorNote>
        ) : null}
        {item.data.proposal_note ? <Note>{item.data.proposal_note}</Note> : null}

        {card ? (
          <section className="card">
            <div className="card-head">
              <span className="icon-tile">
                <Icon name="sparkle" />
              </span>
              <h3 className="h-card">Research card</h3>
            </div>
            <p className="pw-lead">{card.summary}</p>
          </section>
        ) : null}

        {paper?.abstract ? (
          <section className="card soft">
            <div className="card-head">
              <span className="icon-tile powder">
                <Icon name="paper" />
              </span>
              <h3 className="h-card">Abstract</h3>
            </div>
            <p className="pw-body">{paper.abstract}</p>
          </section>
        ) : null}
      </div>

      <div className="pw-col">
        {triage ? (
          <section className="card">
            <div className="card-head">
              <span className={`icon-tile ${triage.relevant ? "mint" : "blush"}`}>
                <Icon name="target" />
              </span>
              <h3 className="h-card spacer">Triage</h3>
              <Chip tone={triage.relevant ? "green" : "gray"}>{triage.relevant ? "Relevant" : "Not relevant"}</Chip>
            </div>
            {triage.why ? <p className="pw-body">{triage.why}</p> : null}
            {triage.model ? (
              <div className="small muted" style={{ marginTop: 8 }}>
                Model: {triage.model}
              </div>
            ) : null}
          </section>
        ) : null}

        {card?.benchmarks?.length ? (
          <section className="card soft">
            <div className="card-head">
              <span className="icon-tile mint">
                <Icon name="bars" />
              </span>
              <h3 className="h-card">Benchmarks in the paper</h3>
            </div>
            <div className="row">
              {card.benchmarks.map((b) => (
                <Chip key={b} tone="outline">
                  {b}
                </Chip>
              ))}
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
