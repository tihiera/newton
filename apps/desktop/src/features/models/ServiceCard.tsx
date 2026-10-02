// One model service, kept short: its model and machine, its state, and Stop / Logs.
// Never its credentials.

import { useState } from "react";
import { api, type Host, type Service } from "../../api";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner, StateChip } from "../../components/ui";
import { ServiceLogs } from "./ServiceLogs";

export const ENDED_SERVICE = new Set(["stopped", "failed", "lost", "rejected", "cancelled"]);

type Panel = "stop" | "logs" | null;

export function ServiceCard({
  service,
  hosts,
  refresh,
}: {
  service: Service;
  hosts: Host[] | undefined;
  refresh: () => void;
}) {
  const [panel, setPanel] = useState<Panel>(null);
  const stop = useAction(async () => {
    await api.services.stop(service.id);
    setPanel(null);
    refresh();
  });
  const host = hosts?.find((h) => h.id === service.host_id);
  const ended = ENDED_SERVICE.has(service.state);
  const toggle = (p: Panel) => setPanel(panel === p ? null : p);

  return (
    <div className="card service-card">
      <div className="service-head">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="service-model-name mono">{service.spec.model}</div>
          <div className="small muted">{host?.name ?? service.host_id}</div>
        </div>
        <StateChip kind="service" state={service.state} />
        {!ended ? (
          <button className="icon-btn" onClick={() => toggle("stop")} aria-label="Stop" title="Stop">
            <Icon name="stop" size={16} />
          </button>
        ) : null}
        <button
          className={`icon-btn ${panel === "logs" ? "active" : ""}`}
          onClick={() => toggle("logs")}
          aria-label="Logs"
          title="Logs"
        >
          <Icon name="logs" size={16} />
        </button>
      </div>

      {service.error ? <ErrorNote error={service.error} /> : null}
      {stop.error ? <ErrorNote error={stop.error} /> : null}

      {panel === "stop" ? (
        <div className="confirm-box">
          <div className="small">Stop {service.spec.model} now?</div>
          <div className="row">
            <button className="btn danger" disabled={stop.busy} onClick={() => stop.run()}>
              {stop.busy ? <Spinner /> : null}
              Stop
            </button>
            <button className="btn ghost" onClick={() => setPanel(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {panel === "logs" ? <ServiceLogs serviceId={service.id} /> : null}
    </div>
  );
}
