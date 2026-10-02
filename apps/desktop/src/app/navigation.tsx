// What is selected and what is open: the only state the app keeps for itself
// (everything scientific comes from agentd).

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type Overlay =
  | { kind: "approvals" }
  | { kind: "compute" }
  | { kind: "models" }
  | { kind: "settings" }
  | { kind: "new-research" }
  | { kind: "ingest"; goalId?: string | null }
  | { kind: "review"; approvalId: string }
  /** "New experiment from built-in schemes" (for a goal when opened from one). */
  | { kind: "new-experiment"; goalId?: string | null }
  | null;

export interface NavState {
  /** null: the global library ("Papers"); otherwise a research goal. */
  goalId: string | null;
  paperId: string | null;
  /** An experiment shown on its own (one with no paper, or opened from an approval). */
  experimentId: string | null;
  inboxCollapsed: boolean;
  overlay: Overlay;
}

interface Nav extends NavState {
  showPapers: () => void;
  showGoal: (goalId: string) => void;
  selectPaper: (paperId: string | null) => void;
  /** Show one experiment in the workspace (null: back to the goal or the library). */
  showExperiment: (experimentId: string | null) => void;
  toggleInbox: () => void;
  open: (overlay: Overlay) => void;
  close: () => void;
}

const NavContext = createContext<Nav | null>(null);

export function NavigationProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<NavState>({
    goalId: null,
    paperId: null,
    experimentId: null,
    inboxCollapsed: false,
    overlay: null,
  });
  const showPapers = useCallback(
    () => setState((s) => ({ ...s, goalId: null, paperId: null, experimentId: null })),
    [],
  );
  const showGoal = useCallback(
    (goalId: string) => setState((s) => ({ ...s, goalId, paperId: null, experimentId: null })),
    [],
  );
  const selectPaper = useCallback(
    (paperId: string | null) => setState((s) => ({ ...s, paperId, experimentId: null })),
    [],
  );
  const showExperiment = useCallback(
    (experimentId: string | null) => setState((s) => ({ ...s, experimentId, paperId: null })),
    [],
  );
  const toggleInbox = useCallback(() => setState((s) => ({ ...s, inboxCollapsed: !s.inboxCollapsed })), []);
  const open = useCallback((overlay: Overlay) => setState((s) => ({ ...s, overlay })), []);
  const close = useCallback(() => setState((s) => ({ ...s, overlay: null })), []);
  const value = useMemo(
    () => ({ ...state, showPapers, showGoal, selectPaper, showExperiment, toggleInbox, open, close }),
    [state, showPapers, showGoal, selectPaper, showExperiment, toggleInbox, open, close],
  );
  return <NavContext.Provider value={value}>{children}</NavContext.Provider>;
}

export function useNav(): Nav {
  const nav = useContext(NavContext);
  if (!nav) throw new Error("useNav outside NavigationProvider");
  return nav;
}
