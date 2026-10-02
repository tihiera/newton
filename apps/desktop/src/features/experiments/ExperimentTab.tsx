// The paper's "Experiment" tab: its experiments (newest first) or, when there are
// none, what can be done: propose one from the card, or why it can't be tested.

import { useMemo, useState } from "react";
import type { ResearchItem } from "../../api";
import { useExperiments } from "../../app/data";
import { ErrorNote, Spinner } from "../../components/ui";
import { when } from "../../components/time";
import { ExperimentDetail } from "./ExperimentDetail";
import { NoExperiment } from "./NoExperiment";
import { experimentsFor } from "./view";
import "./experiments.css";

export function ExperimentTab({ item }: { item: ResearchItem }) {
  const all = useExperiments();
  const mine = useMemo(() => experimentsFor(all.data ?? [], item.id), [all.data, item.id]);
  const [picked, setPicked] = useState<string | null>(null);
  const current = mine.find((e) => e.id === picked) ?? mine[0];

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

  return (
    <div className="stack ex-tab">
      {mine.length > 1 ? (
        <div className="row ex-picker" role="tablist" aria-label="Experiments for this paper">
          {mine.map((e) => (
            <button
              key={e.id}
              role="tab"
              aria-selected={e.id === current.id}
              className={`btn ${e.id === current.id ? "mesh-selected" : "ghost"}`}
              onClick={() => setPicked(e.id)}
            >
              {when(e.created_at)}
            </button>
          ))}
        </div>
      ) : null}
      <ExperimentDetail key={current.id} experimentId={current.id} />
    </div>
  );
}
