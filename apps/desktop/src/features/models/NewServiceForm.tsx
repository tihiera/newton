// Run a model on a machine: POST /services with typed settings. agentd decides if it
// fits (409 says why not) and whether it needs approval (a download does).

import { useEffect, useState } from "react";
import { api, type Approval, type Host, type Service } from "../../api";
import { useNav } from "../../app/navigation";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Note, Spinner } from "../../components/ui";

const ENGINES = ["ollama", "vllm", "mlx", "fake"] as const;

interface Form {
  host_id: string;
  name: string;
  engine: string;
  model: string;
  revision: string;
  memory_gb: string;
  context_length: string;
  parallel: string;
}

const EMPTY: Form = { host_id: "", name: "", engine: "ollama", model: "", revision: "", memory_gb: "", context_length: "", parallel: "" };

const numberOrUndefined = (s: string) => (s.trim() === "" ? undefined : Number(s));

export function NewServiceForm({ hosts, onCreated, onClose }: {
  hosts: Host[] | undefined;
  onCreated: () => void;
  onClose: () => void;
}) {
  const nav = useNav();
  const [form, setForm] = useState<Form>(() => ({ ...EMPTY, host_id: hosts?.find((h) => h.kind === "ssh")?.id ?? hosts?.[0]?.id ?? "" }));
  const [created, setCreated] = useState<{ service: Service; approval: Approval | null } | null>(null);
  useEffect(() => {
    if (!form.host_id && hosts?.length) {
      setForm((f) => ({ ...f, host_id: hosts.find((h) => h.kind === "ssh")?.id ?? hosts[0].id }));
    }
  }, [hosts, form.host_id]);
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const create = useAction(async () => {
    const settings: Record<string, unknown> = {
      engine: form.engine,
      model: form.model.trim(),
      revision: form.revision.trim() || null,
      memory_gb: numberOrUndefined(form.memory_gb),
      context_length: numberOrUndefined(form.context_length),
      parallel: numberOrUndefined(form.parallel),
    };
    for (const k of Object.keys(settings)) if (settings[k] === undefined) delete settings[k];
    const service = await api.services.create({ host_id: form.host_id, name: form.name.trim(), settings });
    let approval: Approval | null = null;
    if (service.state === "awaiting_approval") {
      const pending = await api.approvals.pending();
      approval = pending.find((a) => a.subject_id === service.id) ?? null;
    }
    setCreated({ service, approval });
    onCreated();
  });

  const err = create.error;
  const fe = (name: string) => fieldError(err, name);
  // A 422 about something without its own input (or a 409/400) shows as one sentence.
  const otherError = Boolean(err) && (!Object.keys(create.fields).length || Object.keys(create.fields).some((k) => !(k in EMPTY)));

  if (created) {
    const { service, approval } = created;
    return (
      <div className="card soft stack">
        {service.state === "awaiting_approval" ? (
          <>
            <Note icon="clock">
              <b>{service.name}</b> needs your approval before it starts (it downloads the model or runs
              model-supplied code). Nothing runs until you approve it.
            </Note>
            <div className="row">
              <button
                className="btn primary"
                onClick={() => nav.open(approval ? { kind: "review", approvalId: approval.id } : { kind: "approvals" })}
              >
                Review approval
              </button>
              <button className="btn ghost" onClick={onClose}>Later</button>
            </div>
          </>
        ) : (
          <>
            <Note icon="check">
              <b>{service.name}</b> was created and is starting. It shows in the services list.
            </Note>
            <div className="row">
              <button className="btn" onClick={() => { setCreated(null); setForm((f) => ({ ...EMPTY, host_id: f.host_id })); }}>
                Add another
              </button>
              <button className="btn ghost" onClick={onClose}>Close</button>
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <form
      className="card soft new-service"
      onSubmit={(e) => {
        e.preventDefault();
        void create.run();
      }}
    >
      <div className="card-head">
        <span className="icon-tile blush" style={{ width: 40, height: 40, borderRadius: 12 }}>
          <Icon name="model" size={20} />
        </span>
        <div className="h-card" style={{ flex: 1 }}>New model service</div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
          <Icon name="x" size={18} />
        </button>
      </div>
      <div className="form-grid">
        <Field label="Machine" error={fe("host_id")}>
          <select className="select" value={form.host_id} onChange={set("host_id")}>
            {(hosts ?? []).map((h) => (
              <option key={h.id} value={h.id}>{h.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Name" error={fe("name")}>
          <input className={`input ${fe("name") ? "invalid" : ""}`} value={form.name} onChange={set("name")} placeholder="reader" />
        </Field>
        <Field label="Engine" error={fe("engine")}>
          <select className="select" value={form.engine} onChange={set("engine")}>
            {ENGINES.map((e) => (
              <option key={e} value={e}>{e}</option>
            ))}
          </select>
        </Field>
        <Field label="Model" error={fe("model")}>
          <input className={`input ${fe("model") ? "invalid" : ""}`} value={form.model} onChange={set("model")} placeholder="llama3.2:3b" spellCheck={false} />
        </Field>
        <div className="span-2">
          <Field
            label="Revision (pinned)"
            error={fe("revision")}
            hint="Required: the Ollama manifest digest, or the 40-hex commit for vLLM and MLX."
          >
            <input className={`input mono ${fe("revision") ? "invalid" : ""}`} value={form.revision} onChange={set("revision")} spellCheck={false} />
          </Field>
        </div>
        <Field label="Memory (GB)" error={fe("memory_gb")}>
          <input className={`input ${fe("memory_gb") ? "invalid" : ""}`} type="number" step="any" min={0} value={form.memory_gb} onChange={set("memory_gb")} />
        </Field>
        <Field label="Context length" error={fe("context_length")} hint="Empty: agentd's default">
          <input className={`input ${fe("context_length") ? "invalid" : ""}`} type="number" min={0} value={form.context_length} onChange={set("context_length")} />
        </Field>
        <Field label="Parallel requests" error={fe("parallel")} hint="Empty: agentd's default">
          <input className={`input ${fe("parallel") ? "invalid" : ""}`} type="number" min={1} value={form.parallel} onChange={set("parallel")} />
        </Field>
      </div>
      {otherError ? <ErrorNote error={err} /> : null}
      <div className="row" style={{ marginTop: 6 }}>
        <button className="btn primary" type="submit" disabled={create.busy || !form.host_id}>
          {create.busy ? <Spinner /> : null}
          Create service
        </button>
        <span className="small muted">Downloads need your approval first.</span>
      </div>
    </form>
  );
}
