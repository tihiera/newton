// Run a model on a machine: POST /services with typed settings. agentd decides if it
// fits (409 says why not) and whether it needs approval (a download does).

import { useState } from "react";
import { api, type Approval, type Host, type Service } from "../../api";
import { useNav } from "../../app/navigation";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Note, Spinner } from "../../components/ui";
import { memoryFor, serviceName, type ModelPick } from "./choices";
import { ModelChoices } from "./ModelChoices";

const ENGINES = ["ollama", "vllm", "mlx", "fake"] as const;

interface Form {
  host_id: string;
  name: string;
  engine: string;
  model: string;
  revision: string;
  memory_gb: string;
  /** The picked model's own memory hint: memory_gb grows from it with the context. */
  base_memory: number | null;
  context_length: string;
  parallel: string;
}

const EMPTY: Form = {
  host_id: "",
  name: "",
  engine: "ollama",
  model: "",
  revision: "",
  memory_gb: "",
  base_memory: null,
  context_length: "8192",
  parallel: "",
};

/** How much text the model reads at once (tokens). 8k is the services' default. */
const CONTEXTS = [4096, 8192, 16384, 32768, 65536, 131072];

const numberOrUndefined = (s: string) => (s.trim() === "" ? undefined : Number(s));

/** `initialModel`: start this model (the reader's "Start it"): picked from the machine
 *  or the catalog as soon as they are known. */
export function NewServiceForm({
  hosts,
  onCreated,
  onClose,
  initialModel,
}: {
  hosts: Host[] | undefined;
  onCreated: () => void;
  onClose: () => void;
  initialModel?: string;
}) {
  const nav = useNav();
  const [form, setForm] = useState<Form>(EMPTY);
  const [created, setCreated] = useState<{ service: Service; approval: Approval | null } | null>(null);
  // Until the user picks one: the first SSH machine (a GPU box), else the first machine.
  const hostId = form.host_id || (hosts?.find((h) => h.kind === "ssh")?.id ?? hosts?.[0]?.id ?? "");
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const memoryOf = (f: Form, base: number | null) =>
    base === null ? f.memory_gb : String(memoryFor(base, Number(f.context_length) || 8192, Number(f.parallel) || 1));
  const pick = (p: ModelPick) =>
    setForm((f) => ({
      ...f,
      engine: p.engine,
      model: p.model,
      revision: p.revision,
      base_memory: p.memory_gb,
      memory_gb: memoryOf(f, p.memory_gb),
      name: f.name || serviceName(p.model),
    }));
  // Context and parallel requests change the memory the service needs.
  const setSized = (k: "context_length" | "parallel") => (e: { target: { value: string } }) =>
    setForm((f) => {
      const next = { ...f, [k]: e.target.value };
      return { ...next, memory_gb: memoryOf(next, f.base_memory) };
    });
  const hostName = hosts?.find((h) => h.id === hostId)?.name ?? "this machine";

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
    const name = form.name.trim() || serviceName(form.model.trim());
    const service = await api.services.create({ host_id: hostId, name, settings });
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
  // Advanced opens by itself when one of its fields was refused.
  const advancedError = ["engine", "revision", "name", "memory_gb", "context_length", "parallel"].some((k) => fe(k));
  const otherError =
    Boolean(err) && (!Object.keys(create.fields).length || Object.keys(create.fields).some((k) => !(k in EMPTY)));

  if (created) {
    const { service, approval } = created;
    return (
      <div className="card soft stack">
        {service.state === "awaiting_approval" ? (
          <>
            <Note icon="clock">
              <b>{service.name}</b> needs your approval before it starts (it downloads the model or runs model-supplied
              code). Nothing runs until you approve it.
            </Note>
            <div className="row">
              <button
                className="btn primary"
                onClick={() => nav.open(approval ? { kind: "review", approvalId: approval.id } : { kind: "approvals" })}
              >
                Review approval
              </button>
              <button className="btn ghost" onClick={onClose}>
                Later
              </button>
            </div>
          </>
        ) : (
          <>
            <Note icon="check">
              <b>{service.name}</b> was created and is starting. It shows in the services list.
            </Note>
            <div className="row">
              <button
                className="btn"
                onClick={() => {
                  setCreated(null);
                  setForm((f) => ({ ...EMPTY, host_id: f.host_id }));
                }}
              >
                Add another
              </button>
              <button className="btn ghost" onClick={onClose}>
                Close
              </button>
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
        <div className="h-card" style={{ flex: 1 }}>
          New service
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
          <Icon name="x" size={18} />
        </button>
      </div>
      <Field label="Machine" error={fe("host_id")}>
        <select className="select" value={hostId} onChange={set("host_id")}>
          {(hosts ?? []).map((h) => (
            <option key={h.id} value={h.id}>
              {h.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Model" error={fe("model") ?? fe("revision")}>
        <ModelChoices
          hostId={hostId}
          hostName={hostName}
          selected={`${form.model}@${form.revision}`}
          onPick={pick}
          autoPick={initialModel}
        />
      </Field>
      <Field
        label="Context"
        error={fe("context_length")}
        hint={`How much of a paper the model reads at once. Longer needs more memory${
          form.memory_gb ? `: ${form.memory_gb} GB` : ""
        }.`}
      >
        <select className="select" value={form.context_length} onChange={setSized("context_length")}>
          {CONTEXTS.map((c) => (
            <option key={c} value={String(c)}>
              {c / 1024}k tokens{c === 8192 ? " (default)" : ""}
            </option>
          ))}
        </select>
      </Field>

      <details className="new-service-advanced" open={advancedError || undefined}>
        <summary className="small muted">Advanced</summary>
        <div className="form-grid" style={{ marginTop: 10 }}>
          <Field label="Model" error={fe("model")}>
            <input
              className={`input ${fe("model") ? "invalid" : ""}`}
              value={form.model}
              onChange={set("model")}
              placeholder="llama3.2:3b"
              spellCheck={false}
            />
          </Field>
          <Field label="Engine" error={fe("engine")}>
            <select className="select" value={form.engine} onChange={set("engine")}>
              {ENGINES.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>
          </Field>
          <div className="span-2">
            <Field label="Revision (pinned)" error={fe("revision")}>
              <input
                className={`input mono ${fe("revision") ? "invalid" : ""}`}
                value={form.revision}
                onChange={set("revision")}
                spellCheck={false}
              />
            </Field>
          </div>
          <Field label="Name" error={fe("name")}>
            <input
              className={`input ${fe("name") ? "invalid" : ""}`}
              value={form.name}
              onChange={set("name")}
              placeholder={form.model ? serviceName(form.model) : "reader"}
            />
          </Field>
          <Field label="Memory (GB)" error={fe("memory_gb")}>
            <input
              className={`input ${fe("memory_gb") ? "invalid" : ""}`}
              type="number"
              step="any"
              min={0}
              value={form.memory_gb}
              onChange={set("memory_gb")}
            />
          </Field>
          <Field label="Parallel requests" error={fe("parallel")}>
            <input
              className={`input ${fe("parallel") ? "invalid" : ""}`}
              type="number"
              min={1}
              value={form.parallel}
              onChange={setSized("parallel")}
            />
          </Field>
        </div>
      </details>
      {otherError ? <ErrorNote error={err} /> : null}
      <div className="row" style={{ marginTop: 6 }}>
        <button className="btn primary" type="submit" disabled={create.busy || !hostId || !form.model.trim()}>
          {create.busy ? <Spinner /> : null}
          Start
        </button>
      </div>
    </form>
  );
}
