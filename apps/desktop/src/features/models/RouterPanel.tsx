// The router right now: requests in flight and waiting, and the device leases that
// pause a machine's models while a timed run has it to itself.

import type { Host, RouterStatus } from "../../api";
import { Icon } from "../../components/Icon";
import { ErrorNote } from "../../components/ui";

export function RouterPanel({
  status,
  error,
  hosts,
}: {
  status: RouterStatus | undefined;
  error: Error | undefined;
  hosts: Host[] | undefined;
}) {
  const hostName = (id: string) => hosts?.find((h) => h.id === id)?.name ?? id;
  return (
    <div className="stack" style={{ gap: 12 }}>
      {error && !status ? <ErrorNote error={error} /> : null}
      <div className="facts">
        <div className="fact">
          <div className="fact-label">In flight</div>
          <div className="fact-value">{status?.in_flight ?? "—"}</div>
        </div>
        <div className="fact">
          <div className="fact-label">Waiting</div>
          <div className="fact-value">{status?.waiting ?? "—"}</div>
        </div>
        <div className="fact">
          <div className="fact-label">Models</div>
          <div className="fact-value">{status?.models.length ?? "—"}</div>
        </div>
      </div>
      {(status?.leases ?? []).map((l) => (
        <div key={`${l.host_id}:${l.job_id}`} className="note warn lease">
          <Icon name="pause" size={18} />
          <div className="stack" style={{ gap: 4 }}>
            <div>
              Paused for a timed run: job <span className="mono">{l.job_id}</span> on {hostName(l.host_id)}
            </div>
            <div className="small">
              {l.granted
                ? "The timed run has the machine to itself."
                : `The timed run is waiting on ${l.waiting_on ?? "the router"}.`}
              {l.in_flight ? ` ${l.in_flight} request(s) in flight.` : ""}
            </div>
            {l.caveats.map((c) => (
              <div key={c} className="small">
                {c}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
