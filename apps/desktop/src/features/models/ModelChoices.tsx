// "Choose a model" in the new-service form: what the chosen machine already has, then
// the starter catalog to download. Picking one fills the form; typing still works.

import { useEffect, useRef } from "react";
import { api, type CatalogModel, type MachineModel } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { bytes } from "../../components/time";
import { ErrorNote, Spinner } from "../../components/ui";
import { catalogNotOnMachine, findPick, pickOf, type ModelPick } from "./choices";

export function ModelChoices({
  hostId,
  hostName,
  selected,
  onPick,
  autoPick,
}: {
  hostId: string;
  hostName: string;
  /** model@revision currently in the form, to mark it. */
  selected: string;
  onPick: (pick: ModelPick) => void;
  /** Pick this model once the lists are known (the reader's "Start it"). */
  autoPick?: string;
}) {
  const onMachine = usePolling((s) => api.hosts.models(hostId, s), [hostId], {
    interval: 0,
    followEvents: false,
    enabled: Boolean(hostId),
  });
  const catalog = usePolling((s) => api.models.catalog(s), [], { interval: 0, followEvents: false });
  const here = (onMachine.data ?? []).filter((m) => m.engine === "ollama" || m.engine === "mlx");
  const download = catalogNotOnMachine(catalog.data ?? [], here);

  const picked = useRef(false);
  useEffect(() => {
    if (!autoPick || picked.current || !onMachine.data || !catalog.data) return;
    picked.current = true;
    const p = findPick(autoPick, onMachine.data, catalog.data);
    if (p) onPick(p);
  }, [autoPick, onMachine.data, catalog.data, onPick]);

  // One list: what starts right away first, then what needs a download (with its size).
  const ready = here.filter((m) => m.where === "newton" || m.ready);
  const toDownload = [...here.filter((m) => !(m.where === "newton" || m.ready)), ...download];
  const option = (m: MachineModel | CatalogModel, tag: string, tone: "ready" | "download") => {
    const key = `${m.model}@${m.revision}`;
    return (
      <button
        type="button"
        key={`${key}:${tone}`}
        className={`model-choice ${selected === key ? "selected mesh-selected" : ""}`}
        onClick={() => onPick(pickOf(m))}
        aria-pressed={selected === key}
      >
        <span className="model-choice-name mono">{m.model}</span>
        <span className={`model-choice-tag ${tone}`}>{tag}</span>
      </button>
    );
  };

  return (
    <div className="model-choices">
      {onMachine.error ? <ErrorNote error={onMachine.error} /> : null}
      {catalog.error ? <ErrorNote error={catalog.error} /> : null}
      {!onMachine.data && !onMachine.error ? (
        <div className="row small muted">
          <Spinner /> Looking at {hostName}…
        </div>
      ) : (
        <div className="model-choice-list">
          {ready.map((m) => option(m, "Ready", "ready"))}
          {toDownload.map((m) => option(m, bytes(m.size_bytes), "download"))}
        </div>
      )}
    </div>
  );
}
