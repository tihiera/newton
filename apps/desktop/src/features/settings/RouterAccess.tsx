// The router's inference-only key, for other tools on this Mac. Hidden by default:
// it is fetched only on an explicit Reveal or Copy, and dropped again on Hide.

import { useEffect, useRef, useState } from "react";
import { api, type RouterCredentials } from "../../api";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { ErrorNote, Spinner } from "../../components/ui";

const MASK = "••••••••••••••••••••••••";

type Copied = "url" | "key" | null;

export function RouterAccess() {
  const [creds, setCreds] = useState<RouterCredentials | null>(null);
  const [copied, setCopied] = useState<Copied>(null);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [rotated, setRotated] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const reveal = useAction(async () => {
    setCreds(await api.router.credentials());
  });
  const copy = useAction(async (what: "url" | "key") => {
    const c = creds ?? (await api.router.credentials());
    await navigator.clipboard.writeText(what === "url" ? c.base_url : c.api_key);
    setCopied(what);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(null), 1800);
  });
  const rotate = useAction(async () => {
    const next = await api.router.rotate();
    setConfirmRotate(false);
    setRotated(true);
    // Keep the new key on screen only if the user had revealed the old one.
    setCreds((prev) => (prev ? next : null));
  });
  const error = reveal.error ?? copy.error ?? rotate.error;

  return (
    <section className="settings-card card">
      <div className="small muted" style={{ marginBottom: 14 }}>
        An OpenAI-compatible endpoint for other tools on this Mac. The key only runs inference; it can't change anything
        in Newton.
      </div>
      <div className="secret-rows">
        <div className="secret-row">
          <span className="secret-label">Base URL</span>
          <span className="secret-value mono">{creds ? creds.base_url : MASK}</span>
          <button
            className="icon-btn outlined"
            onClick={() => copy.run("url")}
            disabled={copy.busy}
            aria-label="Copy base URL"
            title="Copy base URL"
          >
            <Icon name={copied === "url" ? "check" : "copy"} size={17} />
          </button>
        </div>
        <div className="secret-row">
          <span className="secret-label">API key</span>
          <span className="secret-value mono">{creds ? creds.api_key : MASK}</span>
          <button
            className="icon-btn outlined"
            onClick={() => copy.run("key")}
            disabled={copy.busy}
            aria-label="Copy API key"
            title="Copy API key"
          >
            <Icon name={copied === "key" ? "check" : "copy"} size={17} />
          </button>
        </div>
      </div>
      {creds?.note ? (
        <div className="small muted" style={{ marginTop: 10 }}>
          {creds.note}
        </div>
      ) : null}
      {copied ? (
        <div className="small muted" style={{ marginTop: 8 }}>
          Copied to the clipboard.
        </div>
      ) : null}
      {rotated && !confirmRotate ? (
        <div className="small muted" style={{ marginTop: 8 }}>
          Key rotated. Tools using the old key must be updated.
        </div>
      ) : null}

      <div className="row" style={{ marginTop: 16 }}>
        {creds ? (
          <button className="btn" onClick={() => setCreds(null)}>
            <Icon name="eye" size={16} /> Hide
          </button>
        ) : (
          <button className="btn" onClick={() => reveal.run()} disabled={reveal.busy}>
            {reveal.busy ? <Spinner /> : <Icon name="eye" size={16} />} Reveal
          </button>
        )}
        <span className="spacer" />
        {!confirmRotate ? (
          <button className="btn ghost" onClick={() => setConfirmRotate(true)}>
            <Icon name="refresh" size={16} /> Rotate key
          </button>
        ) : null}
      </div>
      {confirmRotate ? (
        <div className="confirm-box settings-confirm">
          <div className="small">
            Rotate the router key? The current key stops working at once; tools using it must be given the new one.
          </div>
          <div className="row">
            <button className="btn danger" onClick={() => rotate.run()} disabled={rotate.busy}>
              {rotate.busy ? <Spinner /> : null}
              Rotate key
            </button>
            <button className="btn ghost" onClick={() => setConfirmRotate(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {error ? <ErrorNote error={error} /> : null}
    </section>
  );
}
