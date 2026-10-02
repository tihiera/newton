import type { Approval } from "../../api";
import { Chip } from "../../components/ui";
import { when } from "../../components/time";

/** How an already-decided approval ended. */
export function DecisionLine({ approval }: { approval: Approval }) {
  if (approval.status === "pending") return null;
  const approved = approval.status === "approved";
  return (
    <div className="row ap-decided">
      <Chip tone={approved ? "green" : "gray"}>{approved ? "Approved" : "Rejected"}</Chip>
      {approval.decided_at ? <span className="muted small">{when(approval.decided_at)}</span> : null}
      {approval.decision_note ? <span className="small">“{approval.decision_note}”</span> : null}
    </div>
  );
}
