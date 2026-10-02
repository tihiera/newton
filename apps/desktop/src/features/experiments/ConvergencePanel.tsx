// The convergence plot's data: each job's refinement runs. The experiment view
// carries job results; a finished job without runs there is read from /jobs/{id}.

import { useMemo } from "react";
import { api, type Job } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { ConvergenceChart } from "./ConvergenceChart";
import { convergenceSeries, jobsMissingRuns } from "./view";

export function ConvergencePanel({ jobs }: { jobs: Job[] }) {
  const missing = useMemo(() => jobsMissingRuns(jobs), [jobs]);
  const key = missing.join(",");
  const fetched = usePolling(() => Promise.all(missing.map((id) => api.jobs.get(id))), [key], {
    interval: 0,
    enabled: missing.length > 0,
    followEvents: false,
  });
  const series = useMemo(() => {
    const byId = new Map((fetched.data ?? []).map((j) => [j.id, j]));
    return convergenceSeries(jobs.map((j) => byId.get(j.id) ?? j));
  }, [jobs, fetched.data]);

  if (!series.length) return null;
  return (
    <div className="card ex-chart-card">
      <ConvergenceChart series={series} />
    </div>
  );
}
