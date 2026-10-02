// GitHub and Notion for publishing. Tokens are write-only: typed into a password
// field, sent once to agentd (which keeps them in the Keychain), then forgotten here.

import { useState } from "react";
import { api, type Connectors } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, Spinner } from "../../components/ui";

type Target = "github" | "notion";

const INFO: Record<Target, { name: string; icon: string; tile: string; hint: string }> = {
  github: { name: "GitHub", icon: "github", tile: "lavender", hint: "Publish reports as gists or issues." },
  notion: { name: "Notion", icon: "notion", tile: "blush", hint: "Publish reports as pages under a parent page." },
};

function ConnectorRow({
  target,
  connected,
  refresh,
}: {
  target: Target;
  connected: boolean | undefined;
  refresh: () => void;
}) {
  const info = INFO[target];
  const [token, setToken] = useState("");
  const [confirm, setConfirm] = useState(false);
  const connect = useAction(async () => {
    await api.connectors.connect(target, token.trim());
    setToken("");
    refresh();
  });
  const importGh = useAction(async () => {
    await api.connectors.importGh();
    refresh();
  });
  const disconnect = useAction(async () => {
    await api.connectors.disconnect(target);
    setConfirm(false);
    refresh();
  });
  const error = connect.error ?? importGh.error ?? disconnect.error;
  const busy = connect.busy || importGh.busy || disconnect.busy;

  return (
    <div className="connector">
      <div className="connector-head">
        <span className={`icon-tile ${info.tile}`} style={{ width: 44, height: 44, borderRadius: 14 }}>
          <Icon name={info.icon} size={22} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-card">{info.name}</div>
          <div className="small muted">{info.hint}</div>
        </div>
        {connected === undefined ? null : connected ? (
          <Chip tone="green">
            <span className="dot ok" /> Connected
          </Chip>
        ) : (
          <Chip>
            <span className="dot hollow" /> Not connected
          </Chip>
        )}
      </div>

      {connected === false ? (
        <form
          className="connector-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (token.trim()) void connect.run();
          }}
        >
          <input
            className="input"
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={`Paste a ${info.name} token`}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            aria-label={`${info.name} token`}
          />
          <button className="btn primary" type="submit" disabled={!token.trim() || busy}>
            {connect.busy ? <Spinner /> : null}
            Connect
          </button>
          {target === "github" ? (
            <button className="btn" type="button" disabled={busy} onClick={() => importGh.run()}>
              {importGh.busy ? <Spinner /> : <Icon name="key" size={16} />}
              Use GitHub CLI login
            </button>
          ) : null}
        </form>
      ) : null}

      {connected ? (
        confirm ? (
          <div className="row">
            <span className="small">Disconnect {info.name}? Newton forgets its token.</span>
            <span className="spacer" />
            <button className="btn danger" disabled={busy} onClick={() => disconnect.run()}>
              {disconnect.busy ? <Spinner /> : null}
              Disconnect
            </button>
            <button className="btn ghost" onClick={() => setConfirm(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="row">
            <button className="btn ghost danger" onClick={() => setConfirm(true)}>
              Disconnect
            </button>
          </div>
        )
      ) : null}
      {error ? <ErrorNote error={error} /> : null}
    </div>
  );
}

export function ConnectionsSection() {
  const conns = usePolling<Connectors>(() => api.connectors.get(), [], { interval: 0, followEvents: false });
  return (
    <section className="settings-card card connectors">
      {conns.error && !conns.data ? <ErrorNote error={conns.error} /> : null}
      <ConnectorRow target="github" connected={conns.data?.github} refresh={conns.refresh} />
      <ConnectorRow target="notion" connected={conns.data?.notion} refresh={conns.refresh} />
    </section>
  );
}
