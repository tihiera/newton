// Remove a machine, with an inline confirm. When agentd refuses because work is still
// active on it (400 that names force=true), the user may remove it anyway.

import { useState } from "react";
import { AgentdError, api, type Host } from "../../api";
import { useAction } from "../../hooks/useAction";
import { ErrorNote, Spinner } from "../../components/ui";

export function RemoveHost({ host, onRemoved, onCancel }: { host: Host; onRemoved: () => void; onCancel: () => void }) {
  const [force, setForce] = useState(false);
  const remove = useAction(async (f: boolean) => {
    await api.hosts.remove(host.id, f);
    return true;
  });
  const err = remove.error;
  const offerForce = !force && err instanceof AgentdError && err.status === 400 && /force=true/.test(err.message);

  const go = async (f: boolean) => {
    if (f) setForce(true);
    if (await remove.run(f)) onRemoved();
  };

  return (
    <div className="confirm-box">
      <div className="small">
        Remove <b>{host.name}</b> from Newton? It stops being a place experiments and models can run.
      </div>
      {err ? <ErrorNote error={err} /> : null}
      <div className="row">
        {offerForce ? (
          <button className="btn danger" disabled={remove.busy} onClick={() => go(true)}>
            {remove.busy ? <Spinner /> : null}
            Remove anyway
          </button>
        ) : (
          <button className="btn danger" disabled={remove.busy} onClick={() => go(force)}>
            {remove.busy ? <Spinner /> : null}
            Remove
          </button>
        )}
        <button className="btn ghost" onClick={onCancel} disabled={remove.busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
