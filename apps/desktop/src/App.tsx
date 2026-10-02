// PLACEHOLDER SCREEN, pending the real designs: a connection indicator and a plain
// host list, polled from agentd. Display only; all logic lives in agentd.
import { useEffect, useState } from "react";
import { agentd, AgentdError, type AgentdErrorKind, type Health, type Host } from "./api";

const POLL_MS = 3000;

type Snapshot =
  | { state: "loading" }
  | { state: "ok"; health: Health; hosts: Host[]; at: Date }
  | { state: "error"; kind: AgentdErrorKind; message: string; at: Date };

const ERROR_LABELS: Record<AgentdErrorKind, string> = {
  token_missing: "Token missing",
  unreachable: "agentd not running",
  unauthorized: "Token rejected",
  config: "Shell error",
  http: "agentd error",
};

async function load(): Promise<Snapshot> {
  try {
    const health = await agentd.health();
    const hosts = await agentd.hosts();
    return { state: "ok", health, hosts, at: new Date() };
  } catch (err) {
    if (err instanceof AgentdError) {
      return { state: "error", kind: err.kind, message: err.message, at: new Date() };
    }
    return { state: "error", kind: "http", message: String(err), at: new Date() };
  }
}

function usePolledSnapshot(): Snapshot {
  const [snap, setSnap] = useState<Snapshot>({ state: "loading" });
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const next = await load();
      if (!alive) return;
      setSnap(next);
      timer = setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  return snap;
}

function ConnectionIndicator({ snap }: { snap: Snapshot }) {
  if (snap.state === "loading") return <p data-status="loading">Connecting to agentd…</p>;
  if (snap.state === "ok") {
    return (
      <p data-status="ok">
        ● agentd reachable (v{snap.health.version}, {snap.health.status}), updated{" "}
        {snap.at.toLocaleTimeString()}
      </p>
    );
  }
  return (
    <p data-status="error">
      ● {ERROR_LABELS[snap.kind]}: {snap.message} (retrying every {POLL_MS / 1000}s)
    </p>
  );
}

function Capability({ host, backend }: { host: Host; backend: "cuda" | "metal" }) {
  const cap = host.capabilities?.[backend];
  if (!cap) return <>unknown</>;
  if (cap.ok) return <>ok{cap.device ? ` (${cap.device})` : ""}</>;
  return <>no: {cap.reason ?? "unknown reason"}</>;
}

function HostList({ hosts }: { hosts: Host[] }) {
  if (hosts.length === 0) return <p>No hosts.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Kind</th>
          <th>Status</th>
          <th>CUDA</th>
          <th>Metal</th>
        </tr>
      </thead>
      <tbody>
        {hosts.map((h) => (
          <tr key={h.id}>
            <td>{h.name}</td>
            <td>{h.kind}</td>
            <td>
              {h.status}
              {h.last_error ? ` (${h.last_error})` : ""}
            </td>
            <td>
              <Capability host={h} backend="cuda" />
            </td>
            <td>
              <Capability host={h} backend="metal" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function App() {
  const snap = usePolledSnapshot();
  return (
    <main>
      <header>
        <h1>Newton</h1>
        <small>Placeholder UI, pending the designs.</small>
      </header>
      <ConnectionIndicator snap={snap} />
      <h2>Hosts</h2>
      {snap.state === "ok" ? <HostList hosts={snap.hosts} /> : <p>—</p>}
    </main>
  );
}
