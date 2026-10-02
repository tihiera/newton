// One paper: its card, the method Newton extracted, the claims, and the experiment.

import { useState } from "react";
import { api, type ResearchItem } from "../../api";
import { ErrorNote, Spinner, Tabs } from "../../components/ui";
import { usePolling } from "../../hooks/usePolling";
import { ExperimentTab } from "../experiments/ExperimentTab";
import { ClaimsTab } from "./ClaimsTab";
import { MethodTab } from "./MethodTab";
import { PaperHeader } from "./PaperHeader";
import { SummaryTab } from "./SummaryTab";
import "./papers.css";

type Tab = "summary" | "method" | "claims" | "experiment";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "summary", label: "Summary" },
  { id: "method", label: "Method" },
  { id: "claims", label: "Claims" },
  { id: "experiment", label: "Experiment" },
];

const EXPERIMENT_STATES = new Set(["experiment_planned", "executing", "evaluating", "reported"]);

function firstTab(item: ResearchItem): Tab {
  if (EXPERIMENT_STATES.has(item.state)) return "experiment";
  if (item.data.card) return "method";
  return "summary";
}

export function PaperWorkspace({ paperId }: { paperId: string }) {
  const paper = usePolling((s) => api.research.item(paperId, s), [paperId], { interval: 4000 });
  const [tab, setTab] = useState<Tab | null>(null);
  const item = paper.data;

  if (!item) {
    return (
      <div className="workspace-scroll">
        {paper.error ? (
          <ErrorNote error={paper.error} />
        ) : (
          <div className="empty">
            <Spinner size={22} />
          </div>
        )}
      </div>
    );
  }

  // The first tab follows the paper's state once; after that it's the user's choice.
  if (tab === null) setTab(firstTab(item));
  const current = tab ?? firstTab(item);
  return (
    <div className="workspace-scroll">
      <PaperHeader item={item} />
      <Tabs tabs={TABS} value={current} onChange={setTab} />
      {paper.error ? <ErrorNote error={paper.error} /> : null}
      <div className="fade-in" key={current}>
        {current === "summary" ? <SummaryTab item={item} /> : null}
        {current === "method" ? <MethodTab item={item} onPropose={() => setTab("experiment")} /> : null}
        {current === "claims" ? <ClaimsTab item={item} /> : null}
        {current === "experiment" ? <ExperimentTab item={item} /> : null}
      </div>
    </div>
  );
}
