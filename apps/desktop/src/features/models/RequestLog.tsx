// The router's recent requests: who asked for which model, where it went, status,
// time and tokens. The log holds no prompt or reply content.

import { api, type Host } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { Chip, Empty, ErrorNote } from "../../components/ui";
import { when } from "../../components/time";

function tone(status: number | null): "green" | "red" | "yellow" | "gray" {
  if (status === null) return "gray";
  if (status < 300) return "green";
  if (status === 503 || status === 429) return "yellow";
  return "red";
}

const ms = (n: number | null) => (n === null ? "—" : n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms`);

export function RequestLog({ hosts }: { hosts: Host[] | undefined }) {
  const log = usePolling(() => api.router.requests(30), [], { interval: 5000 });
  const hostName = (id: string | null) => (id ? (hosts?.find((h) => h.id === id)?.name ?? id) : "—");
  const rows = log.data ?? [];

  if (log.error && !log.data) return <ErrorNote error={log.error} />;
  if (log.data && rows.length === 0) {
    return (
      <Empty title="No requests yet" icon="list">
        Requests through the router show here: timings, status and token counts, never content.
      </Empty>
    );
  }
  return (
    <div className="request-table-wrap">
      <table className="request-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Model</th>
            <th>Host</th>
            <th>Status</th>
            <th className="num">Duration</th>
            <th className="num">Tokens</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} title={r.error ?? undefined}>
              <td className="nowrap muted">{when(r.created_at)}</td>
              <td>
                <span className="mono">{r.model_requested ?? "—"}</span>
                {r.model && r.model !== r.model_requested ? (
                  <span className="muted">
                    {" "}
                    → <span className="mono">{r.model}</span>
                  </span>
                ) : null}
                {r.attempts > 1 ? <span className="small muted"> · {r.attempts} attempts</span> : null}
              </td>
              <td className="nowrap">{hostName(r.host_id)}</td>
              <td>
                <Chip tone={tone(r.status)}>{r.status ?? "…"}</Chip>
              </td>
              <td className="num nowrap">{ms(r.duration_ms)}</td>
              <td className="num nowrap">
                {r.prompt_tokens ?? "—"} / {r.completion_tokens ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.some((r) => r.error) ? (
        <div className="small muted" style={{ marginTop: 8 }}>
          Hover a row to read its error.
        </div>
      ) : null}
    </div>
  );
}
