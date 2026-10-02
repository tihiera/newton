// The experiment's publications: where, state, the link once published, and
// agentd's error sentence when one failed, with "Send again" (the same approved text;
// agentd refuses, in its words, if the report changed).

import { useCallback } from "react";
import { api, type Publication } from "../../api";
import { useApprovals } from "../../app/data";
import { useNav } from "../../app/navigation";
import { ExternalLink } from "../../components/ExternalLink";
import { Icon } from "../../components/Icon";
import { ErrorNote, Note, Spinner, StateChip } from "../../components/ui";
import { when } from "../../components/time";
import { useAction } from "../../hooks/useAction";
import { usePolling } from "../../hooks/usePolling";
import { destinationFacts } from "../approvals/text";
import { canSendAgain } from "./destination";
import "./publishing.css";

function PublicationRow({ pub, approvalId }: { pub: Publication; approvalId?: string }) {
  const nav = useNav();
  const f = destinationFacts(pub.target, pub.destination);
  const retry = useAction(useCallback(() => api.publishing.retry(pub.id), [pub.id]));
  return (
    <div className="pub-row">
      <span className="icon-btn outlined" style={{ width: 38, height: 38 }}>
        <Icon name={f.icon} size={18} />
      </span>
      <div style={{ minWidth: 0 }}>
        <div className="row" style={{ gap: 8 }}>
          <span className="pub-row-title">{f.destination}</span>
          {f.where ? <span className="mono muted">{f.where}</span> : null}
          <StateChip kind="publication" state={pub.state} />
        </div>
        <div className="small muted">{when(pub.created_at)}</div>
        {pub.url ? (
          <ExternalLink className="pub-link small" href={pub.url}>
            <Icon name="link" size={14} /> {pub.url}
          </ExternalLink>
        ) : null}
        {pub.error ? (
          <div style={{ marginTop: 8 }}>
            <Note tone="error" icon="alert">
              {pub.error}
            </Note>
          </div>
        ) : null}
        {retry.error ? (
          <div style={{ marginTop: 8 }}>
            <ErrorNote error={retry.error} />
          </div>
        ) : null}
      </div>
      {pub.state === "awaiting_approval" && approvalId ? (
        <button className="btn" onClick={() => nav.open({ kind: "review", approvalId })}>
          Review
        </button>
      ) : canSendAgain(pub) ? (
        <button
          className="btn"
          disabled={retry.busy}
          onClick={() => void retry.run()}
          title="Send the same approved report again"
        >
          {retry.busy ? <Spinner /> : <Icon name="refresh" size={16} />}
          Send again
        </button>
      ) : (
        <span />
      )}
    </div>
  );
}

export function PublicationsList({ experimentId }: { experimentId: string }) {
  const pubs = usePolling(() => api.publishing.list(experimentId), [experimentId], { interval: 5000 });
  const approvals = useApprovals();
  const list = pubs.data ?? [];
  if (!list.length) return null;
  return (
    <div className="card">
      <div className="h-card" style={{ marginBottom: 10 }}>
        Publications
      </div>
      <div className="stack" style={{ gap: 0 }}>
        {list.map((p) => (
          <PublicationRow key={p.id} pub={p} approvalId={approvals.data?.find((a) => a.subject_id === p.id)?.id} />
        ))}
      </div>
    </div>
  );
}
