// One approval in a dialog (mockups 03 and 07): what it is, every detail, and the
// decision. Opened from an experiment, a publication or the approvals drawer.

import { api, type Approval } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Modal, Spinner } from "../../components/ui";
import { usePolling } from "../../hooks/usePolling";
import { ApprovalActions } from "./ApprovalActions";
import { ApprovalDetails, ApprovalFootnote } from "./ApprovalDetails";
import { DecisionLine } from "./DecisionLine";
import { approvalExperimentId, kindText } from "./text";
import "./approvals.css";

async function findApproval(id: string, signal: AbortSignal): Promise<Approval | null> {
  const pending = await api.approvals.pending(signal);
  const hit = pending.find((a) => a.id === id);
  if (hit) return hit;
  const all = await api.approvals.list();
  return all.find((a) => a.id === id) ?? null;
}

export function ReviewDialog({ approvalId, onClose }: { approvalId: string; onClose: () => void }) {
  const nav = useNav();
  const found = usePolling((s) => findApproval(approvalId, s), [approvalId], { interval: 4000 });
  const approval = found.data;
  const experimentId = approval ? approvalExperimentId(approval) : null;
  const openExperiment = experimentId
    ? () => {
        nav.showExperiment(experimentId);
        onClose();
      }
    : null;
  const text = kindText(approval?.kind ?? "");
  const centered = approval?.kind === "publish_report";

  return (
    <Modal onClose={onClose} label={approval ? text.title : "Review"} wide={centered}>
      <div className="modal-body">
        {approval === undefined ? (
          found.error ? (
            <ErrorNote error={found.error} />
          ) : (
            <div className="row muted">
              <Spinner /> Loading the request…
            </div>
          )
        ) : approval === null ? (
          <ErrorNote>This approval no longer exists.</ErrorNote>
        ) : (
          <>
            <div className={`ap-review-head ${centered ? "centered" : ""}`}>
              {centered ? null : (
                <span className={`icon-tile round ${text.tile}`}>
                  <Icon name={text.icon} size={26} />
                </span>
              )}
              <h2 className="h-display" style={{ fontSize: centered ? 38 : 40 }}>
                {text.title}
              </h2>
              <div className="subtitle" style={{ fontSize: 17 }}>
                {text.subtitle}
              </div>
              {approval.title ? <div className="small muted ap-review-title">{approval.title}</div> : null}
            </div>
            <ApprovalDetails approval={approval} />
            {approval.status !== "pending" ? <DecisionLine approval={approval} /> : null}
          </>
        )}
      </div>
      {approval ? (
        <div className="modal-foot ap-modal-foot">
          {approval.status === "pending" ? (
            <ApprovalActions
              approval={approval}
              footnote={<ApprovalFootnote approval={approval} />}
              onDecided={found.refresh}
            />
          ) : (
            <div className="row" style={{ width: "100%" }}>
              <ApprovalFootnote approval={approval} />
              <span className="spacer" />
              {openExperiment ? (
                <button className="btn large" onClick={openExperiment}>
                  Open experiment
                </button>
              ) : null}
              <button className="btn large" onClick={onClose}>
                Close
              </button>
            </div>
          )}
        </div>
      ) : null}
    </Modal>
  );
}
