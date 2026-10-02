// Models to choose from instead of typing: what a machine already has (its worker's
// scan) and the starter catalog. Presentation only: agentd and the worker decide what
// is present, reusable or pinned.

import type { CatalogModel, MachineModel } from "../../api";

/** What picking a model fills in the "new model service" form. */
export interface ModelPick {
  engine: string;
  model: string;
  revision: string;
  memory_gb: number;
}

export function pickOf(m: MachineModel | CatalogModel): ModelPick {
  return { engine: m.engine, model: m.model, revision: m.revision, memory_gb: m.memory_gb_hint };
}

/** Catalog entries not already listed for the machine (same model and revision). */
export function catalogNotOnMachine(catalog: CatalogModel[], onMachine: MachineModel[]): CatalogModel[] {
  return catalog.filter((c) => !onMachine.some((m) => m.model === c.model && m.revision === c.revision));
}

/** Memory for a model at a context: its hint covers 8k for one request; the attention
 *  cache grows with every token of every parallel request (about 6 GB per 32k for a
 *  14B model, at least 4 GB). agentd and the worker still check that it fits. */
export function memoryFor(base: number, context: number, parallel: number): number {
  const tokens = context * Math.max(1, parallel);
  if (tokens <= 8192) return base;
  return base + Math.ceil(((tokens - 8192) / 32768) * Math.max(4, base * 0.5));
}

/** A service name from a model: "llama3.2:3b" -> "llama3-2-3b". */
export function serviceName(model: string): string {
  const base = model.replace(/^mlx-community\//, "").replace(/:latest$/, "");
  return (
    base
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "model"
  );
}

/** The entry to pick for a model name (the reader's "Start it"): this machine's own
 *  copy first, then the catalog. */
export function findPick(
  model: string,
  onMachine: MachineModel[] | undefined,
  catalog: CatalogModel[] | undefined,
): ModelPick | null {
  const plain = (s: string) => s.replace(/:latest$/, "");
  const here = (onMachine ?? []).find((m) => plain(m.model) === plain(model) && (m.where === "newton" || m.ready));
  if (here) return pickOf(here);
  const listed = (catalog ?? []).find((c) => plain(c.model) === plain(model));
  return listed ? pickOf(listed) : null;
}

export interface ReaderGroup {
  label: string;
  options: { model: string; size?: number; detail?: string }[];
}

/** The reader picker's groups: served now, on the machines, to download. A model
 *  appears once, in the first group that has it; the current value is always listed. */
export function readerChoices(
  served: string[],
  onMachines: (MachineModel & { hostName: string })[],
  catalog: CatalogModel[],
  current: string,
): ReaderGroup[] {
  const seen = new Set<string>();
  const take = (model: string) => {
    if (seen.has(model)) return false;
    seen.add(model);
    return true;
  };
  const servedNow = served.filter(take).map((model) => ({ model }));
  const machines = onMachines
    .filter((m) => m.engine === "ollama" || m.engine === "mlx")
    .filter((m) => take(m.model))
    .map((m) => ({ model: m.model, size: m.size_bytes, detail: m.hostName }));
  const download = catalog
    .filter((c) => take(c.model))
    .map((c) => ({ model: c.model, size: c.size_bytes, detail: c.note }));
  const groups: ReaderGroup[] = [
    { label: "Served now", options: servedNow },
    { label: "On your machines", options: machines },
    { label: "Download", options: download },
  ];
  if (current && !seen.has(current)) groups.unshift({ label: "Current", options: [{ model: current }] });
  return groups;
}
