import type { ResearchItem } from "../../api";
import { ExternalLink } from "../../components/ExternalLink";
import { Icon } from "../../components/Icon";
import { stateLabel } from "../../components/labels";
import { when } from "../../components/time";
import { Chip, ErrorNote, Note, Spinner } from "../../components/ui";
import { canReadAgain, doiLinks, journalRef, READING_STATES } from "./format";

/** The card summary, triage, reading state, abstract and where the text came from. */
export function SummaryTab({ item }: { item: ResearchItem }) {
  const { card, paper, triage, text } = item.data;
  // A paper whose triage failed stays "discovered" with agentd's error: not reading.
  const stuck = item.state === "failed" || canReadAgain(item);
  const reading = READING_STATES.has(item.state) && !stuck;
  const journal = journalRef(item);
  const dois = doiLinks(item);

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

        <section className="card soft">
          <div className="card-head">
            <span className="icon-tile lavender">
              <Icon name="database" />
            </span>
            <h3 className="h-card">Source</h3>
          </div>
          <div className="pw-prov-grid" style={{ marginTop: 0 }}>
            <span className="k">arXiv</span>
            <span className="v">{item.external_id}</span>
            {journal ? (
              <>
                <span className="k">Journal</span>
                <span className="v">{journal}</span>
              </>
            ) : null}
            {dois.length ? (
              <>
                <span className="k">{dois.length > 1 ? "DOIs" : "DOI"}</span>
                <span className="v">
                  {dois.map((d, i) => (
                    <span key={`${i}:${d.doi}`}>
                      {i ? " " : null}
                      <ExternalLink href={d.url}>{d.doi}</ExternalLink>
                    </span>
                  ))}
                </span>
              </>
            ) : null}
            {paper?.published ? (
              <>
                <span className="k">Published</span>
                <span className="v">{paper.published.slice(0, 10)}</span>
              </>
            ) : null}
            {paper?.categories?.length ? (
              <>
                <span className="k">Categories</span>
                <span className="v">{paper.categories.join(", ")}</span>
              </>
            ) : null}
            <span className="k">Text</span>
            <span className="v">
              {text ? `${text.from.toUpperCase()} · ${text.characters.toLocaleString()} characters` : "not fetched yet"}
            </span>
            {item.data.model ? (
              <>
                <span className="k">Model</span>
                <span className="v">{item.data.model}</span>
              </>
            ) : null}
            <span className="k">Added</span>
            <span className="v">{when(item.created_at)}</span>
          </div>
        </section>

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

        {paper?.authors?.length ? (
          <section className="card soft">
            <div className="card-head">
              <span className="icon-tile blush">
                <Icon name="list" />
              </span>
              <h3 className="h-card">Authors</h3>
            </div>
            <p className="pw-body">{paper.authors.join(", ")}</p>
          </section>
        ) : null}
      </div>
    </div>
  );
}
