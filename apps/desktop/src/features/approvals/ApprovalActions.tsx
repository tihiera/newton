// Approve / reject one approval, with an optional note. Never automatic: only these
// buttons decide, and agentd's error sentence shows as it is.

import { useCallback, useState, type ReactNode } from "react";
import { api, type Approval } from "../../api";
import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { kindText } from "./text";

export function ApprovalActions({
  approval,
  onDecided,
  footnote,
  compact,
}: {
  approval: Pick<Approval, "id" | "kind" | "status">;
  onDecided?: (result: Approval) => void;
  /** Shown left of the buttons (repo commit, external-action warning). */
  footnote?: ReactNode;
  compact?: boolean;
}) {
  const [note, setNote] = useState("");
  const [choice, setChoice] = useState<"approve" | "reject" | null>(null);
  const decide = useCallback(
    (how: "approve" | "reject") => {
      setChoice(how);
      const trimmed = note.trim() || undefined;
      return how === "approve"
        ? api.approvals.approve(approval.id, trimmed)
        : api.approvals.reject(approval.id, trimmed);
    },
    [approval.id, note],
  );
  const action = useAction(decide);
  const text = kindText(approval.kind);

  if (approval.status !== "pending") return null;

  const go = async (how: "approve" | "reject") => {
    const result = await action.run(how);
    if (result) onDecided?.(result);
  };

  return (
    <div className="stack" style={{ gap: 12 }}>
      <input
        className="input ap-note"
        placeholder="Add a note (optional)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        disabled={action.busy}
        aria-label="Decision note"
      />
      {action.error ? <ErrorNote error={action.error} /> : null}
      <div className="row ap-actions">
        {footnote ? <div className="ap-actions-note">{footnote}</div> : <span className="spacer" />}
        <button
          className={`btn ${compact ? "" : "large"} ap-reject`}
          disabled={action.busy}
          onClick={() => go("reject")}
        >
          {action.busy && choice === "reject" ? <Spinner /> : null}
          Reject
        </button>
        <button
          className={`btn primary ${compact ? "" : "large"}`}
          disabled={action.busy}
          onClick={() => go("approve")}
        >
          {action.busy && choice === "approve" ? (
            <Spinner />
          ) : text.approveIcon ? (
            <Icon name={text.approveIcon} size={17} />
          ) : null}
          {text.approve}
        </button>
      </div>
    </div>
  );
}
