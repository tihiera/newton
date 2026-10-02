// Nothing renders until agentd answers. When it doesn't, one full page says why
// (not running, not configured, token rejected) and keeps retrying, slower each time.

import type { ReactNode } from "react";
import { AgentdError, api, type AgentdErrorKind } from "../api";
import { Icon } from "../components/Icon";
import { Spinner } from "../components/ui";
import { usePolling } from "../hooks/usePolling";

const TITLES: Record<AgentdErrorKind, string> = {
  token_missing: "agentd isn't running yet",
  unreachable: "agentd isn't running",
  unauthorized: "agentd rejected Newton's token",
  config: "Newton can't find agentd",
  http: "agentd answered with an error",
};

const HINTS: Record<AgentdErrorKind, ReactNode> = {
  token_missing: (
    <>
      Start it from the repository with <code>scripts/dev.sh</code> (or run <code>newton-agentd serve</code>). Newton
      connects as soon as it is up.
    </>
  ),
  unreachable: (
    <>
      Start it with <code>scripts/dev.sh</code>. Newton keeps trying and connects as soon as it answers.
    </>
  ),
  unauthorized: (
    <>
      The app and agentd use different data directories. Use <code>pnpm dev:repo</code> with <code>scripts/dev.sh</code>
      , or the same <code>NEWTON_DATA_DIR</code> for both.
    </>
  ),
  config: <>The Newton shell couldn't tell where agentd listens. Check NEWTON_PORT and NEWTON_DATA_DIR.</>,
  http: <>agentd is up but its health check failed.</>,
};

export function AppConnectionBoundary({ children }: { children: ReactNode }) {
  const health = usePolling(() => api.health(), [], {
    interval: 5000,
    followEvents: false,
  });

  if (health.data && !health.error) return <>{children}</>;
  if (health.data && health.error instanceof AgentdError && !health.error.isConnection) return <>{children}</>;

  if (!health.error) {
    return (
      <div className="connection-page">
        <div className="row muted">
          <Spinner /> Connecting to agentd…
        </div>
      </div>
    );
  }
  const kind: AgentdErrorKind = health.error instanceof AgentdError ? health.error.kind : "config";
  return (
    <div className="connection-page">
      <div className="card connection-card mesh-card fade-in">
        <div className="row" style={{ marginBottom: 18 }}>
          <span className="logo-mark" />
          <span className="brand-name">Newton</span>
        </div>
        <h1 className="h-display" style={{ fontSize: 28 }}>
          {TITLES[kind]}
        </h1>
        <p className="subtitle" style={{ fontSize: 15 }}>
          {HINTS[kind]}
        </p>
        <div className="note" style={{ marginTop: 20 }}>
          <Icon name="info" size={18} />
          <div>
            <div className="small muted">What Newton saw</div>
            <div>{health.error.message}</div>
          </div>
        </div>
        <div className="row muted small" style={{ marginTop: 18 }}>
          <Spinner size={14} /> Retrying automatically
          <span className="spacer" />
          <button className="btn" onClick={health.refresh}>
            Try now
          </button>
        </div>
      </div>
    </div>
  );
}
