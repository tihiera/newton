// A host's two settings: GPU support (remote hosts only; the Mac's comes with the
// app) and how many jobs it runs at once. Saved with PATCH /hosts/{id}.

import { useState } from "react";
import { api, type Host } from "../../api";
import { useAction } from "../../hooks/useAction";
import { ErrorNote, Field, fieldError, formError, Spinner } from "../../components/ui";

const GPU_OPTIONS = [
  { value: "auto", label: "Automatic" },
  { value: "cuda", label: "CUDA" },
  { value: "off", label: "Off" },
];

export function HostSettings({ host, onSaved, onClose }: { host: Host; onSaved: () => void; onClose: () => void }) {
  const [gpu, setGpu] = useState<string>(host.gpu_support ?? "auto");
  const [parallel, setParallel] = useState<string>(String(host.max_parallel_jobs ?? ""));
  const save = useAction(async () => {
    const body: { gpu_support?: string; max_parallel_jobs?: number } = {};
    if (host.kind === "ssh" && gpu !== host.gpu_support) body.gpu_support = gpu;
    const n = Number(parallel);
    if (parallel.trim() && n !== host.max_parallel_jobs) body.max_parallel_jobs = n;
    if (Object.keys(body).length) await api.hosts.update(host.id, body);
    return true;
  });

  return (
    <div className="host-settings">
      {host.kind === "ssh" ? (
        <Field label="GPU support" error={fieldError(save.error, "gpu_support")}>
          <select className="select" value={gpu} onChange={(e) => setGpu(e.target.value)}>
            {GPU_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      <Field label="Jobs at once" error={fieldError(save.error, "max_parallel_jobs")}>
        <input
          className={`input ${fieldError(save.error, "max_parallel_jobs") ? "invalid" : ""}`}
          type="number"
          min={1}
          value={parallel}
          onChange={(e) => setParallel(e.target.value)}
        />
      </Field>
      {formError(save.error, ["gpu_support", "max_parallel_jobs"]) ? <ErrorNote error={save.error} /> : null}
      <div className="row">
        <button
          className="btn primary"
          disabled={save.busy}
          onClick={async () => {
            if (await save.run()) {
              onSaved();
              onClose();
            }
          }}
        >
          {save.busy ? <Spinner /> : null}
          Save
        </button>
        <button className="btn ghost" onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  );
}
