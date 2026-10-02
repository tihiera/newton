// The middle column: the papers of the selected research (or all papers), with a
// local search and the way to add one from arXiv.

import { useMemo, useState } from "react";
import { useGoals, usePapers } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { Spinner } from "../../components/ui";
import { matchesQuery } from "./format";
import { PaperRow } from "./PaperRow";
import "./papers.css";

export function PaperInbox() {
  const nav = useNav();
  const goals = useGoals();
  const papers = usePapers(nav.goalId);
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");

  const goal = nav.goalId ? goals.data?.find((g) => g.id === nav.goalId) : undefined;
  const title = nav.goalId === null ? "All papers" : (goal?.title ?? "Research papers");
  const items = papers.data;
  const shown = useMemo(() => (items ?? []).filter((it) => matchesQuery(it, query)), [items, query]);
  const addPaper = () => nav.open({ kind: "ingest", goalId: nav.goalId });

  if (nav.inboxCollapsed) {
    return (
      <aside className="panel inbox collapsed" aria-label="Papers">
        <div className="inbox-head">
          <button className="icon-btn" onClick={nav.toggleInbox} aria-label="Show papers" title="Show papers">
            <Icon name="chevronRight" />
          </button>
        </div>
      </aside>
    );
  }

  const toggleSearch = () => {
    if (searching) setQuery("");
    setSearching(!searching);
  };

  return (
    <aside className="panel inbox" aria-label={title}>
      <div className="inbox-head">
        <h2 className="inbox-title" title={title}>
          {title}
        </h2>
        <button
          className="icon-btn"
          onClick={toggleSearch}
          aria-label={searching ? "Close search" : "Search papers"}
          aria-pressed={searching}
          title="Search"
        >
          <Icon name={searching ? "x" : "search"} />
        </button>
        <button className="icon-btn" onClick={addPaper} aria-label="Add arXiv paper" title="Add arXiv paper">
          <Icon name="plus" />
        </button>
        <button className="icon-btn" onClick={nav.toggleInbox} aria-label="Hide papers" title="Hide papers">
          <Icon name="chevronLeft" />
        </button>
      </div>

      {searching ? (
        <div className="inbox-search">
          <label className="search">
            <Icon name="search" size={16} />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Title, author, summary, arXiv id"
              aria-label="Search papers"
              onKeyDown={(e) => e.key === "Escape" && toggleSearch()}
            />
          </label>
        </div>
      ) : null}

      <div className="inbox-list">
        {papers.data === undefined && papers.loading ? (
          <div className="inbox-empty">
            <Spinner />
          </div>
        ) : papers.error && papers.data === undefined ? (
          <div className="inbox-empty small">{papers.error.message}</div>
        ) : !items?.length ? (
          <div className="inbox-empty">
            <span className="icon-tile round lavender">
              <Icon name="paper" size={24} />
            </span>
            <div>
              {nav.goalId
                ? "No papers yet. Poll now to search arXiv, or add one you already know."
                : "No papers yet. Add one from arXiv to get a research card."}
            </div>
            <button className="btn" onClick={addPaper}>
              <Icon name="plus" size={16} />
              Add arXiv paper
            </button>
          </div>
        ) : shown.length === 0 ? (
          <div className="inbox-empty">No paper matches “{query}”.</div>
        ) : (
          shown.map((it) => (
            <PaperRow key={it.id} item={it} selected={nav.paperId === it.id} onSelect={nav.selectPaper} />
          ))
        )}
      </div>
    </aside>
  );
}
