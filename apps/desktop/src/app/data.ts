// Shared reads used in several places (sidebar, inbox, workspace, drawers). Each is a
// usePolling call: visible-only, abortable, refreshed by /events.

import { api } from "../api";
import { usePolling } from "../hooks/usePolling";

export const useGoals = () => usePolling((s) => api.goals.list(s), [], { interval: 8000 });

export const usePapers = (goalId: string | null) =>
  usePolling((s) => api.research.items(goalId, s), [goalId], { interval: 5000 });

export const useApprovals = () => usePolling((s) => api.approvals.pending(s), [], { interval: 4000 });

export const useHosts = () => usePolling((s) => api.hosts.list(s), [], { interval: 6000 });

export const useProfile = () => usePolling((s) => api.profile.get(s), [], { interval: 10000 });

export const useServices = () => usePolling((s) => api.services.list(s), [], { interval: 5000 });

export const useRouterStatus = () => usePolling((s) => api.router.status(s), [], { interval: 5000 });

export const useExperiments = () => usePolling((s) => api.experiments.list(undefined, s), [], { interval: 5000 });
