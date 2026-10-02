// Nothing renders until agentd answers. While Newton waits for its engine: the logo,
// breathing, and "Loading" (retried every second while the shell says it is starting,
// else slower each time). A failure waiting can't fix (the engine failed, the token was
// rejected, no engine to find): one short line and one button.

import { useEffect, type ReactNode } from "react";
import { AgentdError, api, type AgentdErrorKind } from "../api";
import { Icon } from "../components/Icon";
import { Spinner } from "../components/ui";
import { useAction } from "../hooks/useAction";
import { usePolling } from "../hooks/usePolling";
import { connectionView } from "./engineView";
import { inShell, restartEngine } from "./platform";

export function AppConnectionBoundary({ children }: { children: ReactNode }) {
  const health = usePolling(() => api.health(), [], { interval: 5000, followEvents: false });
  const restart = useAction(restartEngine);
  const error = health.error;
  const kind: AgentdErrorKind = error instanceof AgentdError ? error.kind : "config";
  const view = connectionView(kind, { shell: inShell() });
  const connected = !!health.data && (!error || (error instanceof AgentdError && !error.isConnection));

  // While the engine starts, ask again every second instead of backing off. Each failed
  // read is a new error object, so this re-arms after every try.
  const { refresh } = health;
  const retryMs = !connected && error && view.page === "waiting" ? view.retryMs : null;
  useEffect(() => {
    if (retryMs === null) return;
    const t = setTimeout(refresh, retryMs);
    return () => clearTimeout(t);
  }, [retryMs, error, refresh]);

  if (connected) return <>{children}</>;

  if (!error || view.page === "waiting") {
    return (
      <div className="connection-page" aria-busy="true">
        <div className="connection-wait">
          <span className="logo-mark breathing" />
          <div className="connection-loading">Loading</div>
        </div>
      </div>
    );
  }

  const act = async () => {
    if (view.action === "restart") await restart.run();
    refresh();
  };
  return (
    <div className="connection-page">
      <div className="connection-wait">
        <span className="logo-mark" />
        <div className="connection-line">{view.line}</div>
        <button className="btn primary" disabled={restart.busy} onClick={() => void act()}>
          {restart.busy ? <Spinner /> : <Icon name="refresh" size={16} />}
          {view.action === "restart" ? "Restart" : "Try again"}
        </button>
      </div>
    </div>
  );
}
