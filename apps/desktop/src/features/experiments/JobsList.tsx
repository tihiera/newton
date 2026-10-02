// The experiment's jobs: state, attempt, times, agentd's explanation of any wait or
// failure, and collapsible live logs.

import { useState } from "react";
import type { Job } from "../../api";
import { Icon } from "../../components/Icon";
import { Chip, Note, StateChip } from "../../components/ui";
import { duration, when } from "../../components/time";
import { JobLogs } from "./JobLogs";
import { TERMINAL_JOB } from "./view";

const FAILED = new Set(["failed", "timed_out"]);

function JobCard({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  const finished = TERMINAL_JOB.has(job.state);
  const ran = job.started_at && job.finished_at ? job.finished_at - job.started_at : null;
  return (
    <div className="card soft ex-job">
      <div className="ex-job-head">
        <span className={`icon-tile ${job.role === "baseline" ? "powder" : "blush"}`} style={{ width: 40, height: 40 }}>
          <Icon name={job.role === "baseline" ? "target" : "sparkle"} size={19} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <span className="h-card">{job.label}</span>
            <Chip tone="outline">{job.role}</Chip>
            <StateChip kind="job" state={job.state} />
          </div>
          <div className="small muted ex-job-times">
            {job.attempt > 1 ? <span>Attempt {job.attempt}</span> : null}
            {job.started_at ? <span>Started {when(job.started_at)}</span> : null}
            {job.finished_at ? <span>Finished {when(job.finished_at)}</span> : null}
            {ran !== null ? <span>{duration(ran)}</span> : null}
          </div>
        </div>
        <button className="btn ghost" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <Icon name="logs" size={17} />
          Logs
          <Icon name={open ? "chevronDown" : "chevronRight"} size={16} />
        </button>
      </div>
      {job.error ? (
        <div style={{ marginTop: 10 }}>
          <Note tone={FAILED.has(job.state) ? "error" : "warn"} icon={FAILED.has(job.state) ? "alert" : "clock"}>
            {job.error}
          </Note>
        </div>
      ) : null}
      {open ? <JobLogs jobId={job.id} finished={finished} /> : null}
    </div>
  );
}

export function JobsList({ jobs, live }: { jobs: Job[]; live: boolean }) {
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="row">
        <div className="h-card">Jobs</div>
        {live ? <span className="small muted">updating live</span> : null}
      </div>
      {jobs.map((j) => (
        <JobCard key={j.id} job={j} />
      ))}
    </div>
  );
}
