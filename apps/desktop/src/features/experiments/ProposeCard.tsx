// "Propose experiment": agentd builds the experiment from the paper's card; the user
// only picks the baseline, the initial condition and (optionally) where it runs.
// Then the review dialog opens on the new experiment's approval.

import { useCallback, useState } from "react";
import { api, type ResearchItem } from "../../api";
import { useHosts } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";

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

export function ProposeCard({ item }: { item: ResearchItem }) {
  const nav = useNav();
  const hosts = useHosts();
  const [baseline, setBaseline] = useState("upwind");
  const [ic, setIc] = useState("sine");
  const [hostId, setHostId] = useState("auto");
  const [backend, setBackend] = useState("auto");

  const propose = useCallback(async () => {
    const exp = await api.research.propose(item.id, {
      baseline,
      initial_condition: ic,
      host_id: hostId,
      backend,
    });
    const approval = exp.approval ?? (await api.experiments.get(exp.id)).approval;
    return approval?.id ?? null;
  }, [item.id, baseline, ic, hostId, backend]);
  const action = useAction(propose);

  const submit = async () => {
    const approvalId = await action.run();
    if (approvalId) nav.open({ kind: "review", approvalId });
  };

  const scheme = item.data.scheme_ir;
  return (
    <div className="card mesh-card ex-propose">
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
      {action.error ? <ErrorNote error={action.error} /> : null}
      <button className="btn primary large block" style={{ marginTop: 16 }} disabled={action.busy} onClick={submit}>
        {action.busy ? <Spinner /> : <Icon name="play" size={18} />}
        Propose experiment
      </button>
    </div>
  );
}
