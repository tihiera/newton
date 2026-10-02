// Pick the reader model from a list: what the router serves now, what the machines
// already have, and the starter catalog; or type a name. A model that isn't served yet
// can be started from the reader card ("Start it").

import { useState } from "react";
import { api } from "../../api";
import { useHosts } from "../../app/data";
import { usePolling } from "../../hooks/usePolling";
import { bytes } from "../../components/time";
import { readerChoices } from "./choices";

export function DefaultModelPicker({
  value,
  onChange,
  invalid,
}: {
  value: string;
  onChange: (model: string) => void;
  invalid?: boolean;
}) {
  const served = usePolling(() => api.router.models(), [], { interval: 0, followEvents: false });
  const catalog = usePolling((s) => api.models.catalog(s), [], { interval: 0, followEvents: false });
  const hosts = useHosts();
  const hostIds = (hosts.data ?? []).map((h) => h.id).join(",");
  const onMachines = usePolling(
    async (s) => {
      const list = hosts.data ?? [];
      const answers = await Promise.allSettled(list.map((h) => api.hosts.models(h.id, s)));
      return list.flatMap((h, i) => {
        const a = answers[i];
        return a.status === "fulfilled" ? a.value.map((m) => ({ ...m, hostName: h.name })) : [];
      });
    },
    [hostIds],
    { interval: 0, followEvents: false, enabled: Boolean(hostIds) },
  );
  const groups = readerChoices(
    (served.data?.data ?? []).map((m) => m.id),
    onMachines.data ?? [],
    catalog.data ?? [],
    value,
  );
  const [typed, setTyped] = useState(false);

  if (typed) {
    return (
      <div className="model-picker">
        <input
          className={`input ${invalid ? "invalid" : ""}`}
          value={value}
          placeholder="e.g. llama3.2:3b"
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
          autoFocus
        />
        <button type="button" className="btn ghost small-link" onClick={() => setTyped(false)}>
          Choose from the list
        </button>
      </div>
    );
  }

  return (
    <div className="model-picker">
      <select className={`select ${invalid ? "invalid" : ""}`} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Not set</option>
        {groups.map((g) =>
          g.options.length ? (
            <optgroup key={g.label} label={g.label}>
              {g.options.map((o) => (
                <option key={`${g.label}:${o.model}`} value={o.model}>
                  {o.size ? `${o.model} · ${bytes(o.size)}${o.detail ? ` · ${o.detail}` : ""}` : o.model}
                </option>
              ))}
            </optgroup>
          ) : null,
        )}
      </select>
      <button type="button" className="btn ghost small-link" onClick={() => setTyped(true)}>
        Type a model name
      </button>
    </div>
  );
}
