// Pick the reader model: one of the models the router serves (GET /v1/models), or a
// name typed by hand (a model that isn't served yet).

import { useState } from "react";
import { api } from "../../api";
import { usePolling } from "../../hooks/usePolling";

export function DefaultModelPicker({
  value,
  onChange,
  invalid,
}: {
  value: string;
  onChange: (model: string) => void;
  invalid?: boolean;
}) {
  const models = usePolling(() => api.router.models(), [], { interval: 0, followEvents: false });
  const ids = (models.data?.data ?? []).map((m) => m.id);
  const [typed, setTyped] = useState(false);
  const useText = typed || (models.data !== undefined && ids.length === 0);

  if (useText) {
    return (
      <div className="model-picker">
        <input
          className={`input ${invalid ? "invalid" : ""}`}
          value={value}
          placeholder="e.g. llama3.2:3b"
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
        />
        {ids.length ? (
          <button type="button" className="btn ghost small-link" onClick={() => setTyped(false)}>
            Choose a served model
          </button>
        ) : (
          <span className="small muted">No models are served yet; type the name the router should use.</span>
        )}
      </div>
    );
  }

  const options = value && !ids.includes(value) ? [value, ...ids] : ids;
  return (
    <div className="model-picker">
      <select className={`select ${invalid ? "invalid" : ""}`} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Not set</option>
        {options.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
      <button type="button" className="btn ghost small-link" onClick={() => setTyped(true)}>
        Type a model name
      </button>
    </div>
  );
}
