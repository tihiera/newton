// The paper's "Experiment" tab: its latest experiment, the earlier ones to switch to,
// and "New experiment" under a reported one's evidence; or, when there are none, what
// can be done: propose one from the card, or why it can't be tested. The open form
// belongs to the paper, not to the experiment shown: it sits at the top of the tab, so
// its choices and agentd's scientific-memory warning survive looking at another one.

import { useMemo, useState } from "react";
import type { ResearchItem } from "../../api";
import { useExperiments } from "../../app/data";
import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner } from "../../components/ui";
import { EarlierExperiments } from "./EarlierExperiments";
import { ExperimentDetail } from "./ExperimentDetail";
import { NoExperiment } from "./NoExperiment";
import { ProposeCard } from "./ProposeCard";
import { canPropose, experimentsFor, proposeOpen, shownExperiment } from "./view";
import "./experiments.css";

/** `startProposing`: opened from the Method tab's "Propose experiment", so the form is
 *  open straight away on a paper that already has an experiment. */
export function ExperimentTab({ item, startProposing = false }: { item: ResearchItem; startProposing?: boolean }) {
  const all = useExperiments();
  const mine = useMemo(() => experimentsFor(all.data ?? [], item.id), [all.data, item.id]);
  const [picked, setPicked] = useState<string | null>(null);
  // The paper the "New experiment" form was opened for (null: closed).
  const [openFor, setOpenFor] = useState<string | null>(startProposing ? item.id : null);
  const proposing = proposeOpen(openFor, item);
  // Another paper, or agentd no longer takes a propose (proposed elsewhere): close it,
  // so it doesn't come back already open the next time the paper is reported.
  if (openFor !== null && !proposing) setOpenFor(null);
  const current = shownExperiment(mine, picked);

  if (!all.data) {
    return all.error ? (
      <ErrorNote error={all.error} />
    ) : (
      <div className="row muted">
        <Spinner /> Loading experiments…
      </div>
    );
  }
  if (!current) return <NoExperiment item={item} />;

  const latest = mine[0];
  const next = !canPropose(item) || proposing ? null : (
    <div>
      <button className="btn ex-new" onClick={() => setOpenFor(item.id)}>
        <Icon name="flask" size={18} />
        New experiment
      </button>
    </div>
  );

  return (
    <div className="stack ex-tab">
      {proposing ? (
        <ProposeCard
          key={item.id}
          item={item}
          onCancel={() => setOpenFor(null)}
          onProposed={() => {
            setOpenFor(null);
            setPicked(null); // the new experiment is the latest once the list re-reads
          }}
        />
      ) : null}
      {current.id !== latest.id ? (
        <div className="row small muted ex-not-latest">
          <span style={{ flex: 1 }}>An earlier experiment for this paper.</span>
          <button className="btn ghost" onClick={() => setPicked(null)}>
            Show the latest
          </button>
        </div>
      ) : null}
      <EarlierExperiments experiments={mine.slice(1)} shownId={current.id} onShow={setPicked} />
      <ExperimentDetail key={current.id} experimentId={current.id} next={next} />
    </div>
  );
}
