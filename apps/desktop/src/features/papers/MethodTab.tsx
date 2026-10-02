import type { ResearchItem } from "../../api";
import { Icon } from "../../components/Icon";
import { KeyValue } from "../../components/ui";
import { canPropose, yesNo } from "./format";
import { NoCard } from "./NoCard";
import { ProvenanceCard } from "./ProvenanceCard";
import { SchemeMapping } from "./SchemeMapping";

const dash = (v: string | number | null | undefined) =>
  v === null || v === undefined || v === "" ? "—" : String(v).replace(/_/g, " ");

/** Mockup 02: the extracted method, its mapping onto the IR, and its provenance. */
export function MethodTab({ item, onPropose }: { item: ResearchItem; onPropose: () => void }) {
  const card = item.data.card;
  if (!card) return <NoCard item={item} />;
  const m = card.method;

  return (
    <div className="pw-grid">
      <section className="card">
        <div className="card-head">
          <span className="icon-btn outlined" style={{ width: 42, height: 42 }}>
            <Icon name="paper" />
          </span>
          <h3 className="h-card">Extracted method</h3>
        </div>
        {m.name ? <div className="pw-method-name">{m.name}</div> : null}
        <p className="pw-body" style={{ marginBottom: 14 }}>
          {card.summary}
        </p>
        <KeyValue
          rows={[
            { icon: "cube", label: "Scheme", value: dash(m.name) },
            { icon: "wave", label: "Limiter", value: dash(m.limiter) },
            { icon: "branch", label: "Second-order correction", value: yesNo(m.second_order_correction) },
            { icon: "clock", label: "Time integration", value: dash(m.time_integration) },
            { icon: "bars", label: "Expected order", value: dash(m.order) },
            { icon: "target", label: "Maximum CFL", value: dash(m.max_cfl) },
            { icon: "shield", label: "TVD claimed", value: yesNo(m.tvd) },
          ]}
        />
      </section>

      <div className="pw-col">
        <SchemeMapping item={item} />
        <ProvenanceCard item={item} />
        {canPropose(item) ? (
          <button className="btn primary large block" onClick={onPropose}>
            <Icon name="play" size={18} />
            Propose experiment
          </button>
        ) : null}
      </div>
    </div>
  );
}
