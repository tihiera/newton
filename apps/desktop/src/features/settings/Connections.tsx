// GitHub and Notion for publishing. Connect signs in through the browser (GitHub's
// device code, Notion's authorize page); agentd runs the flow and keeps the tokens in
// the Keychain, the UI only starts it, shows its state and polls until it ends.
// Pasting a token stays as a fallback: typed into a password field, sent once to
// agentd, then forgotten here.

import { useEffect, useRef, useState } from "react";
import { api, type ConnectFlow, type Connectors } from "../../api";
import { copyText, openExternal } from "../../app/platform";
import { ExternalLink } from "../../components/ExternalLink";
import { usePolling } from "../../hooks/usePolling";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, Note, Spinner } from "../../components/ui";
import {
  accountGlyph,
  accountLine,
  accountOf,
  canConnect,
  cancelNote,
  flowResult,
  flowView,
  justConnected,
  panelFor,
  REAUTH_NOTE,
  resumedFlow,
  rowNotes,
  showsRevokeNote,
  workingConnection,
  type FlowLinks,
  type Target,
} from "./connect";

const INFO: Record<Target, { name: string; icon: string; tile: string; hint: string }> = {
  github: { name: "GitHub", icon: "github", tile: "lavender", hint: "Publish reports as gists or issues." },
  notion: { name: "Notion", icon: "notion", tile: "blush", hint: "Publish reports as pages under a parent page." },
};

const GITHUB_APPS = "https://github.com/settings/applications";

/** An open Connect flow: GitHub's code to type, or Notion's authorize link. `id` makes
 *  each start a new subject for the status poll. */
type Flow = FlowLinks & { id: number };

function TokenForm({
  target,
  fallback,
  busy,
  refresh,
}: {
  target: Target;
  fallback: boolean;
  busy: boolean;
  refresh: () => void;
}) {
  const info = INFO[target];
  const [token, setToken] = useState("");
  const connect = useAction(async () => {
    await api.connectors.connect(target, token.trim());
    setToken("");
    refresh();
  });
  const importGh = useAction(async () => {
    await api.connectors.importGh();
    refresh();
  });
  const error = connect.error ?? importGh.error;
  const disabled = busy || connect.busy || importGh.busy;

  return (
    <>
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
        <button className={`btn ${fallback ? "" : "primary"}`} type="submit" disabled={!token.trim() || disabled}>
          {connect.busy ? <Spinner /> : null}
          {fallback ? "Save token" : "Connect"}
        </button>
        {target === "github" ? (
          <button className="btn" type="button" disabled={disabled} onClick={() => importGh.run()}>
            {importGh.busy ? <Spinner /> : <Icon name="key" size={16} />}
            Use GitHub CLI login
          </button>
        ) : null}
      </form>
      {error ? <ErrorNote error={error} /> : null}
    </>
  );
}

function ConnectorRow({
  target,
  conns,
  refresh,
}: {
  target: Target;
  conns: Connectors | undefined;
  refresh: () => void;
}) {
  const info = INFO[target];
  const [confirm, setConfirm] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [flow, setFlow] = useState<Flow | null>(null);
  const [stopped, setStopped] = useState<string | null>(null);
  const [notOpened, setNotOpened] = useState(false);
  const [copied, setCopied] = useState<"code" | "link" | null>(null);
  const nextId = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const panel = panelFor(conns, target, flow !== null);
  const account = accountOf(conns, target);
  const connected = conns?.[target];

  const open = async (url: string) => {
    setNotOpened(false);
    try {
      await openExternal(url);
    } catch {
      setNotOpened(true);
    }
  };

  const start = useAction(async () => {
    setStopped(null);
    setCopied(null);
    nextId.current += 1;
    if (target === "github") {
      const s = await api.connectors.githubDevice();
      setFlow({ id: nextId.current, github: s });
      await open(s.verification_uri);
    } else {
      const s = await api.connectors.notionAuthorize();
      setFlow({ id: nextId.current, url: s.url });
      await open(s.url);
    }
  });
  // agentd forgets the flow and answers with its state: a sign-in that finished
  // meanwhile shows up as connected after the refresh.
  const cancel = useAction(async () => {
    const f =
      target === "github" ? await api.connectors.githubDeviceCancel() : await api.connectors.notionAuthorizeCancel();
    setFlow(null);
    setNotOpened(false);
    setStopped(cancelNote(f));
    refresh();
  });
  const disconnect = useAction(async () => {
    await api.connectors.disconnect(target);
    setConfirm(false);
    setStopped(null);
    refresh();
  });

  // Once the row is connected (and working), however it got there, the last Connect
  // attempt's notes are over: they must not come back under a fresh Connect button
  // after a Disconnect.
  const working = workingConnection(conns, target);
  const [seen, setSeen] = useState(working);
  if (seen !== working) {
    setSeen(working);
    if (justConnected(seen, working)) {
      setStopped(null);
      start.clear();
      cancel.clear();
    }
  }

  // A sign-in started before this dialog opened (it was closed meanwhile) is still
  // agentd's: read it once, and show it again if it's still waiting.
  usePolling(
    async (signal) => {
      const f =
        target === "github"
          ? await api.connectors.githubDeviceStatus(signal)
          : await api.connectors.notionAuthorizeStatus(signal);
      if (f.state === "pending") {
        nextId.current += 1;
        const links = resumedFlow(target, f);
        setFlow((current) => current ?? { id: nextId.current, ...links });
      }
      return f;
    },
    [target],
    { interval: 0, followEvents: false, enabled: canConnect(conns, target) && working === false },
  );

  // The flow's state, every ~2 s while it is open; agentd says when it ends.
  const status = usePolling<ConnectFlow>(
    async (signal) => {
      const f =
        target === "github"
          ? await api.connectors.githubDeviceStatus(signal)
          : await api.connectors.notionAuthorizeStatus(signal);
      const result = flowResult(f);
      if (result.kind !== "waiting") {
        setFlow(null);
        setNotOpened(false);
        setStopped(result.kind === "stopped" ? result.message : null);
        refresh();
      }
      return f;
    },
    [flow?.id],
    { interval: 2000, enabled: flow !== null, followEvents: false },
  );

  const copy = async (what: "code" | "link", text: string) => {
    if (!(await copyText(text))) return;
    setCopied(what);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(null), 1800);
  };

  const notes = rowNotes(panel, { stopped, start: start.error, cancel: cancel.error, disconnect: disconnect.error });
  const busy = start.busy || cancel.busy || disconnect.busy;
  const view = panel === "signing-in" && flow ? flowView(target, flow) : null;
  const code = view === "code" ? flow?.github : undefined;
  const authorizeUrl = view === "link" ? flow?.url : undefined;
  const offersConnect = panel === "connect" || (panel === "reauth" && canConnect(conns, target));
  const glyph = accountGlyph(account?.icon);
  const line = connected ? accountLine(account) : null;

  return (
    <div className="connector">
      <div className="connector-head">
        <span className={`icon-tile ${info.tile}`} style={{ width: 44, height: 44, borderRadius: 14 }}>
          {connected && glyph ? (
            <span className="connector-glyph" aria-hidden>
              {glyph}
            </span>
          ) : (
            <Icon name={info.icon} size={22} />
          )}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-card">{info.name}</div>
          <div className="small muted connector-sub">{line ?? info.hint}</div>
        </div>
        {connected === undefined ? null : panel === "reauth" ? (
          <Chip tone="yellow">
            <span className="dot warn" /> Sign-in expired
          </Chip>
        ) : connected ? (
          <Chip tone="green">
            <span className="dot ok" /> Connected
          </Chip>
        ) : (
          <Chip>
            <span className="dot hollow" /> Not connected
          </Chip>
        )}
      </div>

      {panel === "reauth" ? (
        <Note tone="warn" icon="alert">
          {REAUTH_NOTE}
        </Note>
      ) : null}

      {offersConnect ? (
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={() => start.run()}>
            {start.busy ? <Spinner /> : <Icon name={info.icon} size={16} />}
            Connect {info.name}
          </button>
          <span className="spacer" />
          <button
            className="btn ghost small-link"
            type="button"
            aria-expanded={showToken}
            onClick={() => setShowToken((v) => !v)}
          >
            <Icon name={showToken ? "chevronDown" : "chevronRight"} size={14} />
            Use a token instead
          </button>
        </div>
      ) : null}

      {code ? (
        <div className="connect-flow">
          <div className="small">Enter this code on GitHub to let Newton publish for you:</div>
          <div className="connect-code-row">
            <span className="connect-code mono" aria-label="GitHub code">
              {code.user_code}
            </span>
            <button
              className="icon-btn outlined"
              onClick={() => copy("code", code.user_code)}
              aria-label="Copy code"
              title="Copy code"
            >
              <Icon name={copied === "code" ? "check" : "copy"} size={17} />
            </button>
          </div>
          <div className="row">
            <button className="btn primary" onClick={() => open(code.verification_uri)}>
              <Icon name="github" size={16} /> Open GitHub
            </button>
            <span className="small muted connect-wait">
              <Spinner size={14} /> Waiting for GitHub…
            </span>
            <span className="spacer" />
            <button className="btn ghost" disabled={cancel.busy} onClick={() => cancel.run()}>
              {cancel.busy ? <Spinner /> : null}
              Cancel
            </button>
          </div>
          {notOpened ? (
            <div className="small muted">
              Your browser didn't open. Go to <span className="mono">{code.verification_uri}</span> and enter the code.
            </div>
          ) : null}
        </div>
      ) : null}

      {authorizeUrl ? (
        <div className="connect-flow">
          <div className="row">
            <span className="small connect-wait">
              <Spinner size={14} /> Finish in your browser: pick the pages Newton may use.
            </span>
            <span className="spacer" />
            <button className="btn" onClick={() => open(authorizeUrl)}>
              <Icon name="notion" size={16} /> Open Notion
            </button>
            <button className="btn ghost" disabled={cancel.busy} onClick={() => cancel.run()}>
              {cancel.busy ? <Spinner /> : null}
              Cancel
            </button>
          </div>
          {notOpened ? (
            <div className="row">
              <span className="small muted">Your browser didn't open. Copy the sign-in link into it instead.</span>
              <button className="btn" onClick={() => copy("link", authorizeUrl)}>
                <Icon name={copied === "link" ? "check" : "copy"} size={16} />
                {copied === "link" ? "Copied" : "Copy link"}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {view === "wait" ? (
        <div className="connect-flow">
          <div className="row">
            <span className="small connect-wait">
              <Spinner size={14} /> Waiting for {info.name}…
            </span>
            <span className="spacer" />
            <button className="btn ghost" disabled={cancel.busy} onClick={() => cancel.run()}>
              {cancel.busy ? <Spinner /> : null}
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {panel === "token" || (panel === "reauth" && !offersConnect) || (offersConnect && showToken) ? (
        <TokenForm target={target} fallback={offersConnect} busy={busy} refresh={refresh} />
      ) : null}

      {panel === "connected" || panel === "reauth" ? (
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
            {showsRevokeNote(target, account) ? (
              <span className="small muted">
                You can also <ExternalLink href={GITHUB_APPS}>revoke Newton on github.com</ExternalLink>.
              </span>
            ) : null}
          </div>
        )
      ) : null}

      {notes.stopped ? <ErrorNote>{notes.stopped}</ErrorNote> : null}
      {flow && status.error ? <ErrorNote error={status.error} /> : null}
      {notes.error ? <ErrorNote error={notes.error} /> : null}
    </div>
  );
}

export function ConnectionsSection() {
  // Re-read on every /events change: a sign-in finished in the browser after its row
  // stopped watching (Cancel, or Settings closed) still shows up as connected.
  const conns = usePolling<Connectors>(() => api.connectors.get(), [], { interval: 0, followEvents: true });
  return (
    <section className="settings-card card connectors">
      {conns.error && !conns.data ? <ErrorNote error={conns.error} /> : null}
      <ConnectorRow target="github" conns={conns.data} refresh={conns.refresh} />
      <ConnectorRow target="notion" conns={conns.data} refresh={conns.refresh} />
    </section>
  );
}
