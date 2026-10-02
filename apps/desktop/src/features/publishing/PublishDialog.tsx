// "Publish report" (mockup 06): choose where; nothing is sent until the
// publish_report approval is approved in the review dialog that opens next.

import { useCallback, useEffect, useState } from "react";
import { api, type Experiment, type NotionPage } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, EvidenceBadge, Field, Modal, Note, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { usePolling } from "../../hooks/usePolling";
import {
  closeListOnEscape,
  connectorOf,
  missingInput,
  needsPageSearch,
  pageListHint,
  publishRequest,
  takePageAnswer,
  type Choice,
  type PageList,
} from "./destination";
import "./publishing.css";

const CHOICES: Array<{ id: Choice; icon: string; title: string; sub: string }> = [
  { id: "gist", icon: "github", title: "GitHub Gist", sub: "Share a standalone report" },
  { id: "issue", icon: "alert", title: "GitHub Issue", sub: "Post to a repository" },
  { id: "notion", icon: "notion", title: "Notion page", sub: "Publish below a parent page" },
];

const NAMES = { github: "GitHub", notion: "Notion" } as const;

export function PublishDialog({
  experiment,
  onClose,
}: {
  experiment: Pick<Experiment, "id" | "evidence" | "title">;
  onClose: () => void;
}) {
  const nav = useNav();
  const [choice, setChoice] = useState<Choice>("gist");
  const [repo, setRepo] = useState("");
  const [page, setPage] = useState("");
  const connectors = usePolling(() => api.connectors.get(), [], { interval: 0 });
  const connector = connectorOf(choice);
  const connected = connectors.data?.[connector];
  const missing = missingInput(choice, repo, page);

  const send = useCallback(async () => {
    const req = publishRequest(choice, repo, page);
    const pub = await api.publishing.publish(experiment.id, req.target, req.destination);
    const pending = await api.approvals.pending();
    return pending.find((a) => a.subject_id === pub.id)?.id ?? null;
  }, [choice, repo, page, experiment.id]);
  const action = useAction(send);
  const [orphan, setOrphan] = useState(false);

  const submit = async () => {
    setOrphan(false);
    const approvalId = await action.run();
    if (approvalId) {
      onClose();
      nav.open({ kind: "review", approvalId });
    } else if (approvalId === null) {
      setOrphan(true);
    }
  };

  return (
    <Modal onClose={onClose} label="Publish report">
      <div className="modal-body">
        <div className="pub-head">
          <h2 className="h-display">Publish report</h2>
          <EvidenceBadge evidence={experiment.evidence} />
        </div>
        <div className="subtitle" style={{ fontSize: 16, marginBottom: 22 }}>
          Choose a destination. Nothing is sent until you approve.
        </div>

        <div className="choice-grid" role="radiogroup" aria-label="Destination">
          {CHOICES.map((c) => {
            const on = c.id === choice;
            const ok = connectors.data?.[connectorOf(c.id)];
            return (
              <button
                key={c.id}
                type="button"
                role="radio"
                aria-checked={on}
                className={`choice ${on ? "selected mesh-selected" : ""}`}
                onClick={() => setChoice(c.id)}
              >
                {on ? (
                  <span className="check-mark">
                    <Icon name="check" size={14} />
                  </span>
                ) : null}
                <Icon name={c.icon} size={36} />
                <div className="pub-choice-title">{c.title}</div>
                <div className="small muted">{c.sub}</div>
                {connectors.data ? (
                  <div className={`pub-conn ${ok ? "ok" : ""}`}>
                    <span className={`dot ${ok ? "ok" : "hollow"}`} />
                    {ok ? "Connected" : "Not connected"}
                  </div>
                ) : null}
              </button>
            );
          })}
        </div>

        <div className="stack pub-fields">
          {choice === "issue" ? (
            <Field label="Repository" hint="owner/name">
              <input
                className="input"
                placeholder="owner/name"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
              />
            </Field>
          ) : null}
          {choice === "notion" && connected ? <ParentPagePicker page={page} onPage={setPage} /> : null}
          {connectors.error ? <ErrorNote error={connectors.error} /> : null}
          {connectors.data && !connected ? (
            <Note tone="warn" icon="link">
              <div className="row" style={{ gap: 12 }}>
                <span>Connect {NAMES[connector]} in Settings to publish here.</span>
                <button
                  className="btn"
                  onClick={() => {
                    onClose();
                    nav.open({ kind: "settings" });
                  }}
                >
                  Open Settings
                </button>
              </div>
            </Note>
          ) : null}
          {action.error ? <ErrorNote error={action.error} /> : null}
          {orphan ? <Note>The publication was created; its approval is waiting in Approvals.</Note> : null}
        </div>
      </div>
      <div className="modal-foot pub-foot">
        <button className="btn large block" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn primary large block"
          disabled={action.busy || !connected || missing !== null}
          onClick={submit}
        >
          {action.busy ? <Spinner /> : null}
          Continue to approval
        </button>
      </div>
    </Modal>
  );
}

const PAGE_ID_HINT = "The parent page's id: 32 hexadecimal characters.";

/** The Notion parent page: picked from the pages the integration can see, or pasted
 *  as an id for one it can't list. agentd validates either. */
function ParentPagePicker({ page, onPage }: { page: string; onPage: (id: string) => void }) {
  const [pasting, setPasting] = useState(false);
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<NotionPage>();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 300);
    return () => clearTimeout(timer);
  }, [query]);
  const pages = usePolling(
    async (signal) => ({ query: debounced, pages: await api.connectors.notionPages(debounced, signal) }),
    [debounced],
    { interval: 0, followEvents: false, enabled: !pasting },
  );
  // The last answer stays on screen while a new search loads; `listed` counts the
  // unfiltered list (it decides whether to offer search at all).
  const [list, setList] = useState<PageList>({});
  // Each answer is taken in once, while rendering (no effect, no flash of the old list).
  const [taken, setTaken] = useState<typeof pages.data>();
  if (pages.data && pages.data !== taken) {
    setTaken(pages.data);
    setList(takePageAnswer(list, pages.data, debounced));
  }
  const { shown, listed } = list;
  const hint = pageListHint(list);
  const selected = picked && picked.id === page ? picked : shown?.find((p) => p.id === page);

  const pick = (p: NotionPage) => {
    setPicked(p);
    onPage(p.id);
    setOpen(false);
  };
  const togglePaste = () => {
    setPasting(!pasting);
    setOpen(false);
  };

  if (pasting) {
    return (
      <div className="stack" style={{ gap: 6 }}>
        <Field label="Parent page" hint={PAGE_ID_HINT}>
          <input
            className="input mono"
            placeholder="0123456789abcdef0123456789abcdef"
            value={page}
            onChange={(e) => onPage(e.target.value)}
          />
        </Field>
        <button type="button" className="pub-switch" onClick={togglePaste}>
          Choose from your Notion pages
        </button>
      </div>
    );
  }

  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="field">
        <span className="field-label" id="pub-parent-label">
          Parent page
        </span>
        <button
          type="button"
          className={`select pub-page-select ${open ? "open" : ""}`}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-labelledby="pub-parent-label"
          onClick={() => setOpen(!open)}
        >
          <Icon name="notion" size={18} />
          <span className={`pub-page-title ${selected ? "" : "muted"}`}>
            {selected ? (
              <>
                {selected.icon ? <span className="pub-page-icon">{selected.icon}</span> : null}
                {selected.title}
              </>
            ) : page.trim() ? (
              <span className="mono">{page.trim()}</span>
            ) : (
              "Choose a page"
            )}
          </span>
          <Icon name="chevronDown" size={16} />
        </button>
      </div>
      {open ? (
        <div className="pub-pages">
          {needsPageSearch(listed ?? 0, query) ? (
            <label className="search pub-pages-search">
              <Icon name="search" size={16} />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search Notion pages"
                aria-label="Search Notion pages"
                onKeyDown={(e) => closeListOnEscape(e, () => setOpen(false))}
              />
              {pages.loading ? <Spinner size={14} /> : null}
            </label>
          ) : null}
          {shown === undefined && pages.loading ? (
            <div className="pub-pages-hint small muted">
              <Spinner size={14} /> Loading your Notion pages
            </div>
          ) : null}
          {shown && shown.length > 0 ? (
            <div className="pub-pages-list" role="listbox" aria-label="Notion pages">
              {shown.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  role="option"
                  aria-selected={p.id === page}
                  className={`pub-page ${p.id === page ? "selected" : ""}`}
                  onClick={() => pick(p)}
                >
                  <span className="pub-page-icon">{p.icon ?? <Icon name="notion" size={15} />}</span>
                  <span className="pub-page-title">{p.title}</span>
                  {p.id === page ? <Icon name="check" size={14} /> : null}
                </button>
              ))}
            </div>
          ) : null}
          {hint === "no_match" ? <div className="pub-pages-hint small muted">No page matches that search.</div> : null}
        </div>
      ) : null}
      {hint === "share" ? <Note icon="link">Share the page with your Notion integration, then reopen.</Note> : null}
      {pages.error ? <ErrorNote error={pages.error} /> : null}
      <button type="button" className="pub-switch" onClick={togglePaste}>
        Paste a page id instead
      </button>
    </div>
  );
}
