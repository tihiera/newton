// How a research item reads in lists and headers. Presentation only: every value
// comes from the item agentd returned; nothing here decides anything scientific.

import type { Experiment, ResearchItem, SchemeIR } from "../../api";

/** The year an arXiv id was submitted: "2401.12345" -> 2024, "math/0501001" -> 2005. */
export function arxivYear(id: string | null | undefined): string | null {
  if (!id) return null;
  const modern = /^(?:arxiv:)?(\d{2})(\d{2})\.\d{4,5}/i.exec(id.trim());
  if (modern) return String(2000 + Number(modern[1]));
  const legacy = /\/(\d{2})\d{2}\d{3}/.exec(id);
  if (legacy) {
    const yy = Number(legacy[1]);
    return String(yy >= 91 ? 1900 + yy : 2000 + yy);
  }
  return null;
}

export function paperYear(item: ResearchItem): string | null {
  const published = item.data.paper?.published;
  if (published && /^\d{4}/.test(published)) return published.slice(0, 4);
  return arxivYear(item.external_id);
}

export function firstAuthor(item: ResearchItem): string | null {
  const authors = item.data.paper?.authors ?? [];
  if (!authors.length) return null;
  return authors.length > 1 ? `${authors[0]} et al.` : authors[0];
}

export function authorList(item: ResearchItem, max = 3): string | null {
  const authors = item.data.paper?.authors ?? [];
  if (!authors.length) return null;
  return authors.length > max ? `${authors.slice(0, max).join(", ")} et al.` : authors.join(", ");
}

/** arXiv's journal reference as the authors wrote it, whitespace tidied; null if none. */
export function journalRef(item: ResearchItem): string | null {
  const ref = item.data.paper?.journal_ref?.replace(/\s+/g, " ").trim();
  return ref || null;
}

/** The year after a journal reference, unless the reference already gives one
 *  ("J. Comput. Phys. 231 (2012)" reads badly followed by "· 2011"). */
function yearAfter(ref: string | null, item: ResearchItem): string | null {
  return ref && /\b(19|20)\d{2}\b/.test(ref) ? null : paperYear(item);
}

/** The inbox meta line: the journal (else the first author, else the first category) · year. */
export function paperMeta(item: ResearchItem): string {
  const journal = journalRef(item);
  const who = journal ?? firstAuthor(item) ?? item.data.paper?.categories?.[0] ?? "arXiv";
  return [who, yearAfter(journal, item)].filter(Boolean).join(" · ");
}

/** The header sub line: arXiv id · journal · year · authors. */
export function paperSubline(item: ResearchItem): string {
  const journal = journalRef(item);
  return [`arXiv ${item.external_id}`, journal, yearAfter(journal, item), authorList(item)]
    .filter(Boolean)
    .join(" · ");
}

export interface DoiLink {
  doi: string;
  url: string;
}

/** The DOIs arXiv gave ("10.1016/j.jcp.2012.01.001"), each with its resolver link. A
 *  DOI never holds whitespace: several separated by spaces (an article and its
 *  erratum) are several DOIs, each linked on its own. */
export function doiLinks(item: ResearchItem): DoiLink[] {
  const field = item.data.paper?.doi?.trim();
  if (!field) return [];
  return field.split(/\s+/).flatMap((raw) => {
    const doi = raw.replace(/^(?:https?:\/\/(?:dx\.)?doi\.org\/|doi:)/i, "");
    if (!doi) return [];
    return [{ doi, url: `https://doi.org/${doi.split("/").map(encodeURIComponent).join("/")}` }];
  });
}

/** The newest reported experiment among a paper's (newest first, as experimentsFor sorts). */
export function latestReported<T extends Pick<Experiment, "state">>(mine: ReadonlyArray<T>): T | undefined {
  return mine.find((e) => e.state === "reported");
}

/** The name suggested for an experiment's export: "<title slug>-<experiment id>.zip". */
export function exportFileName(exp: Pick<Experiment, "id" | "title">): string {
  const slug = exp.title
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/, "");
  const id = exp.id.replace(/[^A-Za-z0-9_-]+/g, "-");
  return `${slug || "experiment"}-${id}.zip`;
}

export function paperTitle(item: ResearchItem): string {
  return item.data.paper?.title || item.title;
}

/** Local search over what the inbox shows: title, authors, card summary, arXiv id. */
export function matchesQuery(item: ResearchItem, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const hay = [
    item.title,
    item.data.paper?.title,
    ...(item.data.paper?.authors ?? []),
    item.data.card?.summary,
    item.external_id,
  ]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
  return q.split(/\s+/).every((word) => hay.includes(word));
}

/** How many items are in each state, in the order the states first appear. */
export function countByState(items: ResearchItem[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const it of items) counts.set(it.state, (counts.get(it.state) ?? 0) + 1);
  return [...counts.entries()];
}

const OPS: Record<string, string> = { add: "+", sub: "−", mul: "×", div: "/" };

/** A(c) of the scheme IR as a formula: 0.5, "c", {op, args} trees; null is upwind. */
export function correctionText(c: unknown, nested = false): string {
  if (c === null || c === undefined) return "none (first-order upwind)";
  if (typeof c === "number") return String(c);
  if (typeof c === "string") return c;
  if (typeof c === "object" && c && "op" in c && "args" in c) {
    const { op, args } = c as { op: string; args: unknown[] };
    const [a, b] = Array.isArray(args) ? args : [];
    const text = `${correctionText(a, true)} ${OPS[op] ?? op} ${correctionText(b, true)}`;
    return nested ? `(${text})` : text;
  }
  return JSON.stringify(c);
}

export function timeMethodText(time: SchemeIR["time"] | undefined): string {
  if (!time) return "—";
  if (time.method === "rk") {
    const stages = time.tableau?.b?.length;
    return stages ? `Runge–Kutta, ${stages} stage${stages === 1 ? "" : "s"}` : "Runge–Kutta";
  }
  return "One step";
}

const PROVENANCE_LABELS: Record<string, string> = {
  model: "Model",
  engine: "Engine",
  host: "Host",
  service: "Service",
  revision: "Revision",
  "request-id": "Router request",
  characters_read: "Characters read",
  at: "Read at",
};

export interface ProvenanceRow {
  key: string;
  label: string;
  value: string | number;
}

/** The extraction provenance as rows, known keys first; nested values are skipped. */
export function provenanceRows(extraction: Record<string, unknown> | undefined | null): ProvenanceRow[] {
  if (!extraction) return [];
  const known = Object.keys(PROVENANCE_LABELS).filter((k) => k in extraction);
  const rest = Object.keys(extraction).filter((k) => !(k in PROVENANCE_LABELS)).sort();
  const rows: ProvenanceRow[] = [];
  for (const key of [...known, ...rest]) {
    const v = extraction[key];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean") continue;
    const label = PROVENANCE_LABELS[key] ?? key.replace(/[-_]/g, " ").replace(/^./, (s) => s.toUpperCase());
    rows.push({ key, label, value: typeof v === "boolean" ? (v ? "yes" : "no") : v });
  }
  return rows;
}

export function yesNo(v: boolean | null | undefined): string {
  return v === true ? "Yes" : v === false ? "No" : "—";
}

/** States in which agentd is still reading the paper. */
export const READING_STATES = new Set(["discovered", "triaged", "extracting"]);
