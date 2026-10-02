// Every pending approval, each with its full details and its decision buttons.

import type { Approval } from "../../api";
import { useApprovals } from "../../app/data";
import { Icon } from "../../components/Icon";
import { Chip, Drawer, Empty, ErrorNote, Spinner } from "../../components/ui";
import { when } from "../../components/time";
import { ApprovalActions } from "./ApprovalActions";
import { ApprovalDetails, ApprovalFootnote } from "./ApprovalDetails";
import { kindText } from "./text";
import "./approvals.css";

function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: () => void }) {
  const text = kindText(approval.kind);
  return (
    <div className="card ap-card">
      <div className="card-head" style={{ alignItems: "flex-start" }}>
        <span className={`icon-tile ${text.tile}`}>
          <Icon name={text.icon} size={22} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-card">{approval.title}</div>
          <div className="row small muted" style={{ marginTop: 4, gap: 8 }}>
            <Chip tone="outline">{text.chip}</Chip>
            <span>{when(approval.created_at)}</span>
          </div>
        </div>
      </div>
      <ApprovalDetails approval={approval} />
      <div style={{ marginTop: 14 }}>
        <ApprovalActions
          approval={approval}
          compact
          footnote={<ApprovalFootnote approval={approval} />}
          onDecided={onDecided}
        />
      </div>
    </div>
  );
}

export function ApprovalsDrawer({ onClose }: { onClose: () => void }) {
  const pending = useApprovals();
  const list = pending.data;
  return (
    <Drawer title="Approvals" onClose={onClose}>
      <div className="subtitle" style={{ marginTop: 0, marginBottom: 18 }}>
        Nothing runs, downloads or publishes until you approve it here.
      </div>
      {pending.error && !list ? <ErrorNote error={pending.error} /> : null}
      {!list && !pending.error ? (
        <div className="row muted">
          <Spinner /> Loading…
        </div>
      ) : null}
      {list && !list.length ? (
        <Empty title="Nothing waiting" icon="check">
          Experiments, model downloads and publications that need your approval show up here.
        </Empty>
      ) : null}
      <div className="stack">
        {list?.map((a) => (
          <ApprovalCard key={a.id} approval={a} onDecided={pending.refresh} />
        ))}
      </div>
    </Drawer>
  );
}
