// How the reader model (profile.default_model) reads: which machine serves it and in
// what state, from the router's model list and the services agentd reports.
// Presentation only; the router decides what is routable or paused.

import type { Profile, RouterStatus, Service } from "../../api";
import { stateLabel, type Tone } from "../../components/labels";

const plain = (m: string) => m.replace(/:latest$/, "");
export const sameModel = (a: string, b: string) => a === b || plain(a) === plain(b);

export interface ReaderLine {
  model: string | null;
  hostId: string | null;
  label: string;
  tone: Tone;
  dot: "ok" | "warn" | "bad" | "hollow";
}

export function readerLine(
  profile: Profile | undefined,
  router: RouterStatus | undefined,
  services: Service[] | undefined,
): ReaderLine {
  const model = profile?.default_model ?? null;
  if (!model) return { model: null, hostId: null, label: "Not set", tone: "gray", dot: "hollow" };
  const entry = router?.models.find((m) => sameModel(m.id, model) || m.newton.services.some((s) => s.id === model));
  const routes = entry?.newton.services ?? [];
  const routable = routes.find((r) => r.routable);
  if (routable) return { model, hostId: routable.host_id, label: "Ready", tone: "green", dot: "ok" };
  const paused = routes.find((r) => r.paused);
  if (paused) return { model, hostId: paused.host_id, label: "Paused for a timed run", tone: "yellow", dot: "warn" };
  const svc = (services ?? []).find((s) => sameModel(s.spec.model, model) || s.id === model);
  if (svc) {
    const [label, tone] = stateLabel("service", svc.state);
    const dot = tone === "green" ? "ok" : tone === "red" ? "bad" : tone === "gray" ? "hollow" : "warn";
    return { model, hostId: svc.host_id, label, tone, dot };
  }
  return { model, hostId: routes[0]?.host_id ?? null, label: "Not served", tone: "gray", dot: "hollow" };
}
