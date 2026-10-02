import { useState } from "react";
import type { ResearchItem } from "../../api";
import { Icon } from "../../components/Icon";
import { when } from "../../components/time";
import { provenanceRows } from "./format";

/** Which model, engine, host and router request read the paper. */
export function ProvenanceCard({ item }: { item: ResearchItem }) {
  const [open, setOpen] = useState(false);
  const rows = provenanceRows(item.data.extraction);
  if (!rows.length) return null;
  const pick = (k: string) => rows.find((r) => r.key === k)?.value;
  const headline = [pick("model"), pick("host")].filter(Boolean).join(" · ") || "Router request";

  return (
    <section className="card">
      <button className="pw-provenance row" style={{ flexWrap: "nowrap", gap: 14 }} onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="icon-btn outlined" style={{ width: 42, height: 42 }}>
          <Icon name="logs" />
        </span>
        <span className="spacer" style={{ minWidth: 0 }}>
          <span className="h-card" style={{ display: "block" }}>Extraction provenance</span>
          <span className="muted" style={{ display: "block", marginTop: 2 }}>
            {headline}
          </span>
        </span>
        <Icon name={open ? "chevronDown" : "chevronRight"} className="muted" />
      </button>
      {open ? (
        <div className="pw-prov-grid">
          {rows.map((r) => (
            <div key={r.key} style={{ display: "contents" }}>
              <span className="k">{r.label}</span>
              <span className={`v ${r.key === "request-id" || r.key === "revision" ? "mono" : ""}`}>
                {r.key === "at" && typeof r.value === "number"
                  ? when(r.value)
                  : r.key === "characters_read" && typeof r.value === "number"
                    ? r.value.toLocaleString()
                    : r.value}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
