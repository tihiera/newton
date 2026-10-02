// What the connect flow shows inside a host card: progress, the host-key question,
// agentd's error, or the result (with the selftest job it queued).

import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner } from "../../components/ui";
import type { ConnectPhase } from "./useHostConnect";

export function ConnectPanel({ state, onTrust, onCancel }: {
  state: ConnectPhase;
  onTrust: () => void;
  onCancel: () => void;
}) {
  switch (state.phase) {
    case "idle":
      return null;
    case "connecting":
      return (
        <div className="compute-progress small muted">
          <Spinner /> Connecting: checking the host key, installing and starting the worker…
        </div>
      );
    case "hostkey":
    case "trusting":
      return (
        <div className="hostkey-card">
          <div className="row" style={{ gap: 12, alignItems: "flex-start", flexWrap: "nowrap" }}>
            <span className="icon-tile lavender" style={{ width: 40, height: 40, borderRadius: 12 }}>
              <Icon name="shield" size={20} />
            </span>
            <div className="stack" style={{ gap: 6 }}>
              <div className="h-card">Is this the machine you expect?</div>
              <div className="small muted">
                Newton verifies host keys and never connects to an unknown one.
              </div>
            </div>
          </div>
          <div className="small" style={{ marginTop: 12 }}>{state.message}</div>
          <ul className="fingerprints">
            {state.fingerprints.map((f) => (
              <li key={f} className="mono">{f}</li>
            ))}
          </ul>
          <div className="row" style={{ marginTop: 14 }}>
            <button className="btn primary" onClick={onTrust} disabled={state.phase === "trusting"}>
              {state.phase === "trusting" ? <Spinner /> : <Icon name="shield" size={17} />}
              Trust &amp; connect
            </button>
            <button className="btn ghost" onClick={onCancel} disabled={state.phase === "trusting"}>
              Cancel
            </button>
          </div>
        </div>
      );
    case "connected":
      return (
        <div className="note compute-ok">
          <Icon name="check" size={18} />
          <div>
            Connected. Self-test queued: <span className="mono">{state.result.selftest_job_id}</span>
          </div>
          <button className="icon-btn" style={{ width: 26, height: 26, marginLeft: "auto" }} onClick={onCancel} aria-label="Dismiss">
            <Icon name="x" size={14} />
          </button>
        </div>
      );
    case "error":
      return <ErrorNote error={state.error} />;
  }
}
