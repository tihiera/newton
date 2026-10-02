// Small presentation components shared by every feature. No data fetching here.

import { useEffect, type ReactNode } from "react";
import { AgentdError, type Evidence } from "../api";
import { Icon } from "./Icon";
import { EVIDENCE, stateLabel, TABLES, type Tone } from "./labels";

export function Chip({
  tone = "gray",
  children,
  large,
  className = "",
}: {
  tone?: Tone | "outline";
  children: ReactNode;
  large?: boolean;
  className?: string;
}) {
  return <span className={`chip ${tone} ${large ? "large" : ""} ${className}`}>{children}</span>;
}

export function StateChip({ kind, state, large }: { kind: keyof typeof TABLES; state: string; large?: boolean }) {
  const [label, tone] = stateLabel(kind, state);
  return (
    <Chip tone={tone} large={large}>
      {label}
    </Chip>
  );
}

export function EvidenceBadge({ evidence, large }: { evidence: Evidence | null | undefined; large?: boolean }) {
  const e = EVIDENCE[evidence ?? "unknown"] ?? EVIDENCE.unknown;
  return (
    <Chip tone={e.tone} large={large}>
      <span
        className={`dot ${evidence === "green" ? "ok" : evidence === "yellow" ? "warn" : evidence === "red" ? "bad" : ""}`}
      />
      {e.label}
    </Chip>
  );
}

/** A soft mesh thumbnail, its colours seeded from the paper's id. */
export function Thumb({ seed, size, className = "" }: { seed: string; size?: number; className?: string }) {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const palette = [45, 330, 260, 210, 15, 190];
  const pick = (n: number) => palette[(h >>> (n * 3)) % palette.length];
  const style = {
    "--h1": pick(0),
    "--h2": pick(1),
    "--h3": pick(2),
    ...(size ? { width: size, height: size, borderRadius: size / 5.5 } : {}),
  } as React.CSSProperties;
  return <div className={`thumb ${className}`} style={style} />;
}

export function ErrorNote({ error, children }: { error?: unknown; children?: ReactNode }) {
  if (!error && !children) return null;
  const text = error instanceof Error ? error.message : error ? String(error) : null;
  return (
    <div className="note error" role="alert">
      <Icon name="alert" size={18} />
      <div>{children ?? text}</div>
    </div>
  );
}

export function Note({
  children,
  tone,
  icon = "info",
}: {
  children: ReactNode;
  tone?: "warn" | "error";
  icon?: string;
}) {
  return (
    <div className={`note ${tone ?? ""}`}>
      <Icon name={icon} size={18} />
      <div>{children}</div>
    </div>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Icon name="refresh" size={size} className="spin" />;
}

export function Field({
  label,
  error,
  children,
  hint,
}: {
  label: string;
  error?: string;
  children: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {error ? <span className="field-error">{error}</span> : hint ? <span className="small muted">{hint}</span> : null}
    </label>
  );
}

/** The message for one form field from a 422 (or nothing). */
export function fieldError(error: unknown, name: string): string | undefined {
  return error instanceof AgentdError ? error.fields[name] : undefined;
}

/** The error to show under a form: everything a field doesn't already show (a 422 on a
 *  key no input displays still reaches the user). */
export function formError(error: Error | undefined, shown: string[]): Error | undefined {
  if (!error) return undefined;
  const keys = error instanceof AgentdError ? Object.keys(error.fields) : [];
  return keys.length === 0 || keys.some((k) => !shown.includes(k)) ? error : undefined;
}

export function Switch({
  on,
  onChange,
  disabled,
  label,
}: {
  on: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={`switch ${on ? "on" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!on)}
    />
  );
}

function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

export function Modal({
  onClose,
  children,
  wide,
  label,
}: {
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
  label: string;
}) {
  useEscape(onClose);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className={`modal mesh-card ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={label}>
        <button className="icon-btn modal-close" onClick={onClose} aria-label="Close">
          <Icon name="x" />
        </button>
        {children}
      </div>
    </>
  );
}

export function Drawer({
  title,
  onClose,
  children,
  footer,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEscape(onClose);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : "Panel"}
      >
        <div className="drawer-head mesh-header">
          <h2 className="h-display" style={{ fontSize: 30, flex: 1 }}>
            {title}
          </h2>
          <button className="icon-btn outlined" onClick={onClose} aria-label="Close">
            <Icon name="x" />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer ? <div className="drawer-foot">{footer}</div> : null}
      </aside>
    </>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: Array<{ id: T; label: string }>;
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={t.id === value}
          className={`tab ${t.id === value ? "active mesh-tab-active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Stepper({ steps, current, failed }: { steps: string[]; current: number; failed?: boolean }) {
  return (
    <div className="stepper">
      {steps.map((label, i) => (
        <div key={label} style={{ display: "contents" }}>
          {i > 0 ? <div className="step-line" /> : null}
          <div
            className={`step ${i < current ? "done" : i === current ? (failed ? "failed current" : "current") : ""}`}
          >
            <div className="bubble">
              {i < current ? (
                <Icon name="check" size={17} />
              ) : i === current && failed ? (
                <Icon name="x" size={17} />
              ) : i === current ? (
                <span className="dot" style={{ background: "var(--ink)" }} />
              ) : null}
            </div>
            {label}
          </div>
        </div>
      ))}
    </div>
  );
}

export function KeyValue({ rows }: { rows: Array<{ icon: string; label: string; value: ReactNode }> }) {
  return (
    <div className="kv">
      {rows.map((r) => (
        <div className="kv-row" key={r.label}>
          <span className="icon-btn outlined" style={{ width: 34, height: 34 }}>
            <Icon name={r.icon} size={17} />
          </span>
          <span className="kv-key">{r.label}</span>
          <span className="kv-val">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

export function Empty({ title, children, icon = "paper" }: { title: string; children?: ReactNode; icon?: string }) {
  return (
    <div className="empty">
      <span className="icon-tile round lavender">
        <Icon name={icon} size={24} />
      </span>
      <div className="h-section">{title}</div>
      {children ? <div style={{ maxWidth: 420 }}>{children}</div> : null}
    </div>
  );
}
