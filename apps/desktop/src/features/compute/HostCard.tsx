// One machine: what it is, what it can run, its status in agentd's words, and the
// actions on it (check, connect, self-test, GPU support, settings, remove).

import { useEffect, useRef, useState } from "react";
import { api, type Host, type Job } from "../../api";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, Spinner, StateChip } from "../../components/ui";
import { ago } from "../../components/time";
import { ConnectPanel } from "./ConnectPanel";
import { HostSettings } from "./HostSettings";
import { RemoveHost } from "./RemoveHost";
import { canInstallGpu, capabilityChips, deviceName, gpuTaskLine, memoryLine, needsConnect, statusChip, workerVersion } from "./hostView";
import { useHostConnect } from "./useHostConnect";

type Panel = "settings" | "remove" | null;

export function HostCard({ host, refresh, autoConnect, onAutoConnectStarted }: {
  host: Host;
  refresh: () => void;
  /** Just added from ~/.ssh/config: connect once, right away (the host key still needs the user). */
  autoConnect?: boolean;
  onAutoConnectStarted?: () => void;
}) {
  const [panel, setPanel] = useState<Panel>(null);
  const [selftest, setSelftest] = useState<Job | null>(null);
  const conn = useHostConnect(host.id, refresh);
  const check = useAction(async () => {
    await api.hosts.check(host.id);
    refresh();
  });
  const test = useAction(async () => {
    const job = await api.hosts.selftest(host.id);
    setSelftest(job);
    refresh();
  });
  const gpu = useAction(async () => {
    await api.hosts.gpuSupport(host.id);
    refresh();
  });

  const started = useRef(false);
  useEffect(() => {
    if (autoConnect && !started.current) {
      started.current = true;
      onAutoConnectStarted?.();
      void conn.connect();
    }
  }, [autoConnect, onAutoConnectStarted, conn]);

  const status = statusChip(host);
  const lines = [deviceName(host), memoryLine(host)].filter(Boolean) as string[];
  const chips = capabilityChips(host);
  const task = gpuTaskLine(host);
  const version = workerVersion(host);
  const connectFirst = needsConnect(host);
  const actionError = check.error ?? test.error ?? gpu.error;

  return (
    <div className="card host-card">
      <div className="host-main">
        <div className={`host-art ${host.kind === "local" ? "local" : "remote"}`}>
          <Icon name={host.kind === "local" ? "laptop" : "server"} size={44} />
        </div>
        <div className="host-info">
          <div className="h-card host-name">{host.name}</div>
          {lines.map((l) => (
            <div key={l} className="host-line">{l}</div>
          ))}
          {host.kind === "ssh" && host.ssh_target ? <div className="host-line mono">{host.ssh_target}</div> : null}
          <div className="row host-chips">
            {chips.map((c) => (
              <Chip key={c.key} tone={c.ok ? "lavender" : "gray"}>{c.label}</Chip>
            ))}
            <Chip tone={status.tone}>
              <span className={`dot ${status.dot}`} />
              {status.label}
            </Chip>
          </div>
        </div>
        <div className="host-primary">
          {connectFirst ? (
            <button className="btn primary" onClick={() => conn.connect()} disabled={conn.busy}>
              {conn.state.phase === "connecting" ? <Spinner /> : null}
              Connect
            </button>
          ) : (
            <button className="btn" onClick={() => check.run()} disabled={check.busy}>
              {check.busy ? <Spinner /> : null}
              Check
            </button>
          )}
        </div>
      </div>

      {task ? (
        task.tone === "busy" ? (
          <div className="compute-progress small muted"><Spinner /> {task.text}</div>
        ) : (
          <ErrorNote>{task.text}</ErrorNote>
        )
      ) : null}

      {chips.filter((c) => !c.ok && c.reason).map((c) => (
        <div key={c.key} className="small muted cap-reason">
          <b>{c.label}:</b> {c.reason}
        </div>
      ))}

      {host.last_error ? <div className="note warn host-error">{host.last_error}</div> : null}

      <div className="host-meta small muted">
        {version ? <span>Worker {version}</span> : null}
        <span>Checked {ago(host.last_checked_at)}</span>
        {host.max_parallel_jobs ? <span>{host.max_parallel_jobs} jobs at once</span> : null}
      </div>

      <div className="row host-actions">
        {connectFirst ? (
          <button className="btn ghost small-btn" onClick={() => check.run()} disabled={check.busy}>
            {check.busy ? <Spinner size={14} /> : <Icon name="refresh" size={15} />} Check
          </button>
        ) : null}
        <button className="btn ghost small-btn" onClick={() => test.run()} disabled={test.busy}>
          {test.busy ? <Spinner size={14} /> : <Icon name="play" size={15} />} Self-test
        </button>
        {canInstallGpu(host) ? (
          <button className="btn ghost small-btn" onClick={() => gpu.run()} disabled={gpu.busy}>
            {gpu.busy ? <Spinner size={14} /> : <Icon name="chip" size={15} />} GPU support
          </button>
        ) : null}
        <button
          className={`btn ghost small-btn ${panel === "settings" ? "active" : ""}`}
          onClick={() => setPanel(panel === "settings" ? null : "settings")}
        >
          <Icon name="settings" size={15} /> Settings
        </button>
        {host.kind === "ssh" ? (
          <button className="btn ghost small-btn danger" onClick={() => setPanel(panel === "remove" ? null : "remove")}>
            <Icon name="trash" size={15} /> Remove
          </button>
        ) : null}
      </div>

      <ConnectPanel state={conn.state} onTrust={() => conn.trust()} onCancel={conn.reset} />
      {actionError ? <ErrorNote error={actionError} /> : null}
      {selftest ? (
        <div className="note compute-ok">
          <Icon name="flask" size={18} />
          <div className="row" style={{ gap: 8 }}>
            Self-test <span className="mono">{selftest.id}</span>
            <StateChip kind="job" state={selftest.state} />
          </div>
          <button className="icon-btn" style={{ width: 26, height: 26, marginLeft: "auto" }} onClick={() => setSelftest(null)} aria-label="Dismiss">
            <Icon name="x" size={14} />
          </button>
        </div>
      ) : null}
      {panel === "settings" ? <HostSettings host={host} onSaved={refresh} onClose={() => setPanel(null)} /> : null}
      {panel === "remove" ? <RemoveHost host={host} onRemoved={refresh} onCancel={() => setPanel(null)} /> : null}
    </div>
  );
}
