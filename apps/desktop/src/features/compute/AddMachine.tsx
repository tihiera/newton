// Add a machine from ~/.ssh/config: pick an alias, Newton adds it and connects
// (the host key is then confirmed by the user on the new host's card).

import { useState } from "react";
import { api, type SshConfigHost } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, Empty, ErrorNote, Field, fieldError, Spinner } from "../../components/ui";

function target(h: SshConfigHost): string {
  const user = h.user ? `${h.user}@` : "";
  const port = h.port ? `:${h.port}` : "";
  return h.hostname ? `${user}${h.hostname}${port}` : h.alias;
}

export function AddMachine({ onAdded }: { onAdded: (hostId: string) => void }) {
  const [open, setOpen] = useState(false);
  const list = usePolling(() => api.hosts.sshConfig(), [], { interval: 0, enabled: open, followEvents: false });
  const [adding, setAdding] = useState<string | null>(null);
  const add = useAction(async (h: SshConfigHost) => {
    setAdding(h.alias);
    try {
      const host = await api.hosts.create({ name: h.alias, ssh_target: h.alias });
      onAdded(host.id);
      list.refresh();
    } finally {
      setAdding(null);
    }
  });

  const [manualName, setManualName] = useState("");
  const [manualTarget, setManualTarget] = useState("");
  const [manualPort, setManualPort] = useState("");
  const manual = useAction(async () => {
    const host = await api.hosts.create({
      name: manualName.trim() || manualTarget.trim(),
      ssh_target: manualTarget.trim(),
      ...(manualPort.trim() ? { ssh_port: Number(manualPort) } : {}),
    });
    setManualName("");
    setManualTarget("");
    setManualPort("");
    onAdded(host.id);
  });

  if (!open) {
    return (
      <button className="btn block add-machine-btn" onClick={() => setOpen(true)}>
        <Icon name="plus" size={18} /> Add a machine
      </button>
    );
  }

  const rows = list.data ?? [];
  return (
    <div className="card soft add-machine">
      <div className="card-head">
        <span className="icon-tile powder" style={{ width: 40, height: 40, borderRadius: 12 }}>
          <Icon name="server" size={20} />
        </span>
        <div style={{ flex: 1 }}>
          <div className="h-card">Add a machine</div>
          <div className="small muted">From your SSH config. Newton connects over SSH; you never open a terminal.</div>
        </div>
        <button className="icon-btn" onClick={() => setOpen(false)} aria-label="Close">
          <Icon name="x" size={18} />
        </button>
      </div>
      {list.error ? <ErrorNote error={list.error} /> : null}
      {add.error ? <ErrorNote error={add.error} /> : null}
      {list.loading && !list.data ? (
        <div className="compute-progress small muted"><Spinner /> Reading ~/.ssh/config…</div>
      ) : rows.length === 0 && list.data ? (
        <Empty title="No hosts in your SSH config" icon="server">
          Add a <span className="mono">Host</span> entry to <span className="mono">~/.ssh/config</span>, then open this again.
        </Empty>
      ) : (
        <div className="ssh-list">
          {rows.map((h) => (
            <div key={`${h.config_file}:${h.alias}`} className="ssh-row">
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="ssh-alias">{h.alias}</div>
                <div className="small muted mono ssh-target">{target(h)}</div>
              </div>
              {h.host_id ? (
                <Chip tone="green">Added</Chip>
              ) : h.addable ? (
                <button className="btn" disabled={adding !== null} onClick={() => add.run(h)}>
                  {adding === h.alias ? <Spinner /> : null}
                  Add
                </button>
              ) : (
                <Chip>Not addable</Chip>
              )}
            </div>
          ))}
        </div>
      )}
      <form
        className="stack manual-add"
        style={{ marginTop: 16, gap: 10 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (manualTarget.trim()) void manual.run();
        }}
      >
        <div className="small muted">Or a machine you reach as user@host</div>
        <div className="row" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}>
          <Field label="SSH target" error={fieldError(manual.error, "ssh_target")}>
            <input
              className={`input mono ${fieldError(manual.error, "ssh_target") ? "invalid" : ""}`}
              placeholder="ghost@100.85.54.5"
              value={manualTarget}
              onChange={(e) => setManualTarget(e.target.value)}
            />
          </Field>
          <Field label="Port" error={fieldError(manual.error, "ssh_port")}>
            <input
              className={`input mono ${fieldError(manual.error, "ssh_port") ? "invalid" : ""}`}
              style={{ width: 84 }}
              inputMode="numeric"
              placeholder="22"
              value={manualPort}
              onChange={(e) => setManualPort(e.target.value)}
            />
          </Field>
          <Field label="Name" error={fieldError(manual.error, "name")}>
            <input className="input" placeholder="Spark" value={manualName} onChange={(e) => setManualName(e.target.value)} />
          </Field>
        </div>
        {manual.error && !Object.keys(manual.fields).length ? <ErrorNote error={manual.error} /> : null}
        <button className="btn" type="submit" disabled={!manualTarget.trim() || manual.busy}>
          {manual.busy ? <Spinner /> : <Icon name="plus" size={16} />} Add machine
        </button>
      </form>
    </div>
  );
}
