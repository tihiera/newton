// The "Compute" drawer (mockup 05): the machines Newton can run on, adding one from
// ~/.ssh/config with the host-key step, and the reader model at the bottom.

import { useCallback, useState } from "react";
import { useHosts } from "../../app/data";
import { Icon } from "../../components/Icon";
import { Drawer, ErrorNote, Spinner } from "../../components/ui";
import { AddMachine } from "./AddMachine";
import { HostCard } from "./HostCard";
import { ReaderSummary } from "./ReaderSummary";
import "./compute.css";

export function ComputeDrawer({ onClose }: { onClose: () => void }) {
  const hosts = useHosts();
  const [autoConnect, setAutoConnect] = useState<string | null>(null);
  const clearAuto = useCallback(() => setAutoConnect(null), []);
  const refreshHosts = hosts.refresh;
  const onAdded = useCallback(
    (hostId: string) => {
      setAutoConnect(hostId);
      refreshHosts();
    },
    [refreshHosts],
  );

  return (
    <Drawer
      title="Compute"
      onClose={onClose}
      footer={
        <button className="btn primary large block" onClick={onClose}>
          Done
        </button>
      }
    >
      <div className="compute">
        <h3 className="h-section compute-heading">Compute hosts</h3>
        {hosts.error && !hosts.data ? <ErrorNote error={hosts.error} /> : null}
        {!hosts.data && hosts.loading ? (
          <div className="compute-progress small muted"><Spinner /> Loading machines…</div>
        ) : null}
        <div className="stack" style={{ gap: 14 }}>
          {(hosts.data ?? []).map((h) => (
            <HostCard
              key={h.id}
              host={h}
              refresh={hosts.refresh}
              autoConnect={autoConnect === h.id}
              onAutoConnectStarted={clearAuto}
            />
          ))}
        </div>
        <div className="compute-note small muted">
          <Icon name="info" size={18} />
          Timed runs execute alone on the selected host.
        </div>
        <AddMachine onAdded={onAdded} />

        <div className="compute-divider" />
        <h3 className="h-section compute-heading">Models</h3>
        <ReaderSummary hosts={hosts.data} />
      </div>
    </Drawer>
  );
}
