// "Publish report" (mockup 06): choose where; nothing is sent until the
// publish_report approval is approved in the review dialog that opens next.

import { useCallback, useState } from "react";
import { api, type Experiment } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, EvidenceBadge, Field, Modal, Note, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { usePolling } from "../../hooks/usePolling";
import { connectorOf, missingInput, publishRequest, type Choice } from "./destination";
import "./publishing.css";

const CHOICES: Array<{ id: Choice; icon: string; title: string; sub: string }> = [
  { id: "gist", icon: "github", title: "GitHub Gist", sub: "Share a standalone report" },
  { id: "issue", icon: "alert", title: "GitHub Issue", sub: "Post to a repository" },
  { id: "notion", icon: "notion", title: "Notion page", sub: "Publish below a parent page" },
];

const NAMES = { github: "GitHub", notion: "Notion" } as const;

export function PublishDialog({ experiment, onClose }: { experiment: Pick<Experiment, "id" | "evidence" | "title">; onClose: () => void }) {
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
              <input className="input" placeholder="owner/name" value={repo} onChange={(e) => setRepo(e.target.value)} />
            </Field>
          ) : null}
          {choice === "notion" ? (
            <Field label="Parent page" hint="The parent page's id: 32 hexadecimal characters.">
              <input
                className="input mono"
                placeholder="0123456789abcdef0123456789abcdef"
                value={page}
                onChange={(e) => setPage(e.target.value)}
              />
            </Field>
          ) : null}
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
          {orphan ? (
            <Note>The publication was created; its approval is waiting in Approvals.</Note>
          ) : null}
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
