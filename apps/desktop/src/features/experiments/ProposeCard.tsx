// "Propose experiment": agentd builds the experiment from the paper's card; the user
// only picks the baseline, the initial condition and (optionally) where it runs.
// Then the review dialog opens on the new experiment's approval. When scientific
// memory refuses (409 already_tested / already_planned) agentd's sentence is shown as
// is, and "Propose anyway" sends what the form shows now with retest: true.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ResearchItem } from "../../api";
import { useHosts } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Note, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { memoryConflict, proposeBody } from "./view";

type ProposeBody = NonNullable<Parameters<typeof api.research.propose>[1]>;

const BASELINES = [
  ["upwind", "Upwind"],
  ["lax_wendroff", "Lax–Wendroff"],
  ["muscl_minmod", "MUSCL · minmod"],
  ["muscl_vanleer", "MUSCL · van Leer"],
] as const;
const INITIAL = [
  ["sine", "Sine wave"],
  ["gaussian", "Gaussian pulse"],
  ["square", "Square wave"],
] as const;
const BACKENDS = [
  ["auto", "Automatic"],
  ["cpu", "CPU"],
  ["cuda", "CUDA"],
  ["metal", "Metal"],
] as const;

/** `onCancel`: the form was opened on purpose (a paper's "New experiment") and can be
 *  closed again. `onProposed`: the experiment exists (before the review opens). */
export function ProposeCard({ item, onCancel, onProposed }: {
  item: ResearchItem;
  onCancel?: () => void;
  onProposed?: () => void;
}) {
  const nav = useNav();
  const hosts = useHosts();
  const [baseline, setBaseline] = useState("upwind");
  const [ic, setIc] = useState("sine");
  const [hostId, setHostId] = useState("auto");
  const [backend, setBackend] = useState("auto");
  const card = useRef<HTMLDivElement>(null);
  // Opened on purpose from the tab's "New experiment": bring it into view.
  const opened = !!onCancel;
  useEffect(() => {
    if (opened) card.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [opened]);

  const propose = useCallback(
    async (body: ProposeBody) => {
      const exp = await api.research.propose(item.id, body);
      const approval = exp.approval ?? (await api.experiments.get(exp.id)).approval;
      return approval?.id ?? null;
    },
    [item.id],
  );
  const action = useAction(propose);
  const conflict = memoryConflict(action.error);

  // Always the choice the form shows, also for "Propose anyway" after a 409.
  const send = async (retest: boolean) => {
    const approvalId = await action.run(proposeBody({ baseline, initial_condition: ic, host_id: hostId, backend }, retest));
    if (approvalId === undefined) return; // failed: the error is shown
    onProposed?.();
    if (approvalId) nav.open({ kind: "review", approvalId });
  };

  const scheme = item.data.scheme_ir;
  return (
    <div className="card mesh-card ex-propose" ref={card}>
      <div className="card-head">
        <span className="icon-tile">
          <Icon name="flask" size={22} />
        </span>
        <div style={{ flex: 1 }}>
          <div className="h-card">Propose experiment</div>
          <div className="small muted">
            Test <span className="mono">{scheme?.name}</span> from this paper against a baseline. You review the plan
            before anything runs.
          </div>
        </div>
        {onCancel ? (
          <button className="icon-btn" onClick={onCancel} aria-label="Close" title="Close">
            <Icon name="x" />
          </button>
        ) : null}
      </div>
      {item.data.scheme_note ? <div className="small muted ex-note-line">{item.data.scheme_note}</div> : null}
      <div className="ex-form">
        <Field label="Baseline" error={fieldError(action.error, "baseline")}>
          <select className="select" value={baseline} onChange={(e) => setBaseline(e.target.value)}>
            {BASELINES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Initial condition" error={fieldError(action.error, "initial_condition")}>
          <select className="select" value={ic} onChange={(e) => setIc(e.target.value)}>
            {INITIAL.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Machine" error={fieldError(action.error, "host_id")}>
          <select className="select" value={hostId} onChange={(e) => setHostId(e.target.value)}>
            <option value="auto">Automatic</option>
            {(hosts.data ?? []).map((h) => (
              <option key={h.id} value={h.id}>
                {h.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Backend" error={fieldError(action.error, "backend")}>
          <select className="select" value={backend} onChange={(e) => setBackend(e.target.value)}>
            {BACKENDS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {conflict ? (
        <div className="ex-memory">
          <Note tone="warn" icon="alert">
            {conflict.message}
          </Note>
          <div className="row ex-memory-actions">
            <button className="btn" disabled={action.busy} onClick={action.clear}>
              Cancel
            </button>
            <button className="btn primary" disabled={action.busy} onClick={() => send(true)}>
              {action.busy ? <Spinner /> : <Icon name="play" size={18} />}
              Propose anyway
            </button>
          </div>
        </div>
      ) : (
        <>
          {action.error ? <ErrorNote error={action.error} /> : null}
          <button className="btn primary large block" style={{ marginTop: 16 }} disabled={action.busy} onClick={() => send(false)}>
            {action.busy ? <Spinner /> : <Icon name="play" size={18} />}
            Propose experiment
          </button>
        </>
      )}
    </div>
  );
}
