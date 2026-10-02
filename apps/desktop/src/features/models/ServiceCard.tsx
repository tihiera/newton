// One model service: what it serves and where, its state and health as agentd
// reports them, whether the router has paused it for a timed run, and stop / drain /
// logs. Never its credentials (the router is the way in).

import { useState } from "react";
import { api, type Host, type RouterModel, type Service } from "../../api";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, Spinner, StateChip } from "../../components/ui";
import { ServiceLogs } from "./ServiceLogs";

const ENDED = new Set(["stopped", "failed", "lost", "rejected", "cancelled"]);

type Panel = "stop" | "drain" | "logs" | null;

export function ServiceCard({
  service,
  hosts,
  routes,
  refresh,
}: {
  service: Service;
  hosts: Host[] | undefined;
  routes: RouterModel[] | undefined;
  refresh: () => void;
}) {
  const [panel, setPanel] = useState<Panel>(null);
  const [seconds, setSeconds] = useState("60");
  const stop = useAction(async () => {
    await api.services.stop(service.id);
    setPanel(null);
    refresh();
  });
  const drain = useAction(async () => {
    await api.services.drain(service.id, Number(seconds));
    setPanel(null);
    refresh();
  });

  const host = hosts?.find((h) => h.id === service.host_id);
  const route = routes?.flatMap((m) => m.newton.services).find((s) => s.id === service.id);
  const ended = ENDED.has(service.state);
  const toggle = (p: Panel) => setPanel(panel === p ? null : p);
  const actionError = stop.error ?? drain.error;

  return (
    <div className="card service-card">
      <div className="service-head">
        <span className="icon-tile lavender">
          <Icon name="model" size={22} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-card service-name">{service.name}</div>
          <div className="small muted service-model">
            {service.spec.engine} · <span className="mono">{service.spec.model}</span>
            {service.spec.revision ? <span className="mono"> @{service.spec.revision.slice(0, 12)}</span> : null}
          </div>
        </div>
        <StateChip kind="service" state={service.state} />
      </div>

      <div className="row service-facts">
        <span className="row small muted" style={{ gap: 6 }}>
          <Icon name={host?.kind === "local" ? "laptop" : "server"} size={16} />
          {host?.name ?? service.host_id}
        </span>
        {service.healthy !== null ? (
          <Chip tone={service.healthy ? "green" : "red"}>
            <span className={`dot ${service.healthy ? "ok" : "bad"}`} />
            {service.healthy ? "Healthy" : "Unhealthy"}
          </Chip>
        ) : null}
        {service.endpoint ? (
          <Chip tone={service.endpoint.reachable === false ? "red" : service.endpoint.reachable ? "blue" : "gray"}>
            {service.endpoint.reachable === false
              ? "Endpoint unreachable"
              : service.endpoint.reachable
                ? "Endpoint reachable"
                : "Endpoint not checked"}
          </Chip>
        ) : null}
        {route?.paused ? (
          <Chip tone="yellow">
            <Icon name="pause" size={13} /> Paused for a timed run
          </Chip>
        ) : null}
        {route ? (
          <span className="small muted">
            {route.in_flight}/{route.parallel} in flight
          </span>
        ) : null}
        <span className="small muted">
          {service.spec.memory_gb} GB · {service.spec.context_length.toLocaleString()} ctx
        </span>
      </div>

      {service.error ? <ErrorNote error={service.error} /> : null}
      {service.auth_note ? <div className="small muted service-note">{service.auth_note}</div> : null}

      <div className="row host-actions">
        {!ended ? (
          <>
            <button className="btn ghost small-btn danger" onClick={() => toggle("stop")}>
              <Icon name="stop" size={15} /> Stop
            </button>
            {service.state === "ready" ? (
              <button
                className={`btn ghost small-btn ${panel === "drain" ? "active" : ""}`}
                onClick={() => toggle("drain")}
              >
                <Icon name="clock" size={15} /> Drain
              </button>
            ) : null}
          </>
        ) : null}
        <button className={`btn ghost small-btn ${panel === "logs" ? "active" : ""}`} onClick={() => toggle("logs")}>
          <Icon name="logs" size={15} /> Logs
          <Icon name={panel === "logs" ? "chevronDown" : "chevronRight"} size={14} />
        </button>
      </div>

      {actionError ? <ErrorNote error={actionError} /> : null}

      {panel === "stop" ? (
        <div className="confirm-box">
          <div className="small">
            Stop <b>{service.name}</b> now?
          </div>
          <div className="row">
            <button className="btn danger" disabled={stop.busy} onClick={() => stop.run()}>
              {stop.busy ? <Spinner /> : null}
              Stop now
            </button>
            <button className="btn ghost" onClick={() => setPanel(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {panel === "drain" ? (
        <div className="confirm-box">
          <div className="small">
            Stop taking new requests now, then stop after this many seconds so requests in flight can finish.
          </div>
          <div className="row">
            <input
              className="input"
              type="number"
              min={0}
              value={seconds}
              onChange={(e) => setSeconds(e.target.value)}
              style={{ width: 110 }}
              aria-label="Seconds to wait"
            />
            <span className="small muted">seconds</span>
            <span className="spacer" />
            <button className="btn primary" disabled={drain.busy || !seconds.trim()} onClick={() => drain.run()}>
              {drain.busy ? <Spinner /> : null}
              Drain
            </button>
          </div>
        </div>
      ) : null}
      {panel === "logs" ? <ServiceLogs serviceId={service.id} /> : null}
    </div>
  );
}
