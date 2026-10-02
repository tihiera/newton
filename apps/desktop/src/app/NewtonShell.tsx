// The window: sidebar | paper inbox | workspace, plus the drawers and dialogs.
// Which workspace shows: a selected paper, else the selected research, else the
// global library.

import { ApprovalsDrawer } from "../features/approvals/ApprovalsDrawer";
import { ReviewDialog } from "../features/approvals/ReviewDialog";
import { ComputeDrawer } from "../features/compute/ComputeDrawer";
import { GoalWorkspace } from "../features/goals/GoalWorkspace";
import { NewResearchDialog } from "../features/goals/NewResearchDialog";
import { ModelsDrawer } from "../features/models/ModelsDrawer";
import { IngestDialog } from "../features/papers/IngestDialog";
import { PaperInbox } from "../features/papers/PaperInbox";
import { PapersHome } from "../features/papers/PapersHome";
import { PaperWorkspace } from "../features/papers/PaperWorkspace";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { Sidebar } from "../features/sidebar/Sidebar";
import { Icon } from "../components/Icon";
import { useApprovals } from "./data";
import { useNav } from "./navigation";

function ApprovalsButton() {
  const nav = useNav();
  const pending = useApprovals();
  const n = pending.data?.length ?? 0;
  return (
    <button className="btn approvals-btn" onClick={() => nav.open({ kind: "approvals" })}>
      <Icon name="clock" size={18} />
      Approvals
      {n ? (
        <span className="chip lavender" style={{ padding: "1px 8px" }}>
          {n}
        </span>
      ) : null}
    </button>
  );
}

export function NewtonShell() {
  const nav = useNav();
  const o = nav.overlay;
  return (
    <div className="shell">
      <div className="drag-strip" data-tauri-drag-region />
      <Sidebar />
      <PaperInbox />
      <main className="panel workspace">
        <div className="approvals-slot">
          <ApprovalsButton />
        </div>
        {nav.paperId ? (
          <PaperWorkspace key={nav.paperId} paperId={nav.paperId} />
        ) : nav.goalId ? (
          <GoalWorkspace key={nav.goalId} goalId={nav.goalId} />
        ) : (
          <PapersHome />
        )}
      </main>

      {o?.kind === "approvals" ? <ApprovalsDrawer onClose={nav.close} /> : null}
      {o?.kind === "review" ? <ReviewDialog approvalId={o.approvalId} onClose={nav.close} /> : null}
      {o?.kind === "compute" ? <ComputeDrawer onClose={nav.close} /> : null}
      {o?.kind === "models" ? <ModelsDrawer onClose={nav.close} /> : null}
      {o?.kind === "settings" ? <SettingsDialog onClose={nav.close} /> : null}
      {o?.kind === "new-research" ? <NewResearchDialog onClose={nav.close} /> : null}
      {o?.kind === "ingest" ? <IngestDialog goalId={o.goalId} onClose={nav.close} /> : null}
    </div>
  );
}
