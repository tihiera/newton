import { api, type Finding, type ResearchItem } from "../../api";
import { Icon } from "../../components/Icon";
import { when } from "../../components/time";
import { Chip, EvidenceBadge } from "../../components/ui";
import { usePolling } from "../../hooks/usePolling";
import { yesNo } from "./format";
import { NoCard } from "./NoCard";

function show(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return yesNo(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** What was measured for this paper's claims, from the findings (scientific memory). */
function Tested({ findings }: { findings: Finding[] }) {
  return (
    <section className="card">
      <div className="card-head">
        <span className="icon-tile mint">
          <Icon name="flask" />
        </span>
        <h3 className="h-card">Tested</h3>
      </div>
      <div className="stack">
        {findings.map((f) => (
          <div key={f.id} className="stack" style={{ gap: 10 }}>
            <div className="row">
              <EvidenceBadge evidence={f.evidence} />
              <span className="small muted">
                {f.scheme_name ?? f.experiment_id} · {when(f.created_at)}
              </span>
            </div>
            <p className="pw-body">{f.summary}</p>
            {f.claims.length ? (
              <div className="row">
                {f.claims.map((c, i) => (
                  <Chip key={`${c.claim}-${i}`} tone={c.holds === true ? "green" : c.holds === false ? "red" : "gray"}>
                    <Icon name={c.holds === true ? "check" : c.holds === false ? "x" : "info"} size={14} />
                    {c.claim}
                    {c.claimed !== undefined && c.claimed !== null ? ` ${show(c.claimed)}` : ""}
                    {c.holds === true ? " holds" : c.holds === false ? " did not hold" : " not checked"}
                  </Chip>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}

export function ClaimsTab({ item }: { item: ResearchItem }) {
  const findings = usePolling((s) => api.research.findings(item.goal_id, s), [item.goal_id], { interval: 0 });
  const card = item.data.card;
  const ir = item.data.scheme_ir;
  const mine = (findings.data ?? []).filter((f) => f.research_item_id === item.id);
  if (!card) return <NoCard item={item} />;

  return (
    <div className="pw-grid">
      <section className="card">
        <div className="card-head">
          <span className="icon-tile blush">
            <Icon name="list" />
          </span>
          <h3 className="h-card">Claims in the paper</h3>
        </div>
        {card.claims.length ? (
          <div>
            {card.claims.map((c, i) => (
              <div className="pw-claim" key={i}>
                <div>
                  <Chip tone="lavender">{c.kind.replace(/_/g, " ")}</Chip>
                </div>
                <div>{c.text}</div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">The card lists no claims.</p>
        )}
      </section>

      <div className="pw-col">
        <section className="card soft">
          <div className="card-head">
            <span className="icon-tile powder">
              <Icon name="target" />
            </span>
            <h3 className="h-card">Claims Newton can check</h3>
          </div>
          {ir ? (
            <div className="facts">
              <div className="fact">
                <div className="fact-label">Order</div>
                <div className="fact-value">{ir.claims.order}</div>
              </div>
              <div className="fact">
                <div className="fact-label">Max CFL</div>
                <div className="fact-value">{ir.claims.max_cfl}</div>
              </div>
              <div className="fact">
                <div className="fact-label">TVD</div>
                <div className="fact-value">{yesNo(ir.claims.tvd)}</div>
              </div>
            </div>
          ) : (
            <p className="pw-body">{item.data.scheme_note ?? "No scheme was mapped, so there is nothing to check."}</p>
          )}
        </section>
        {mine.length ? <Tested findings={mine} /> : null}
      </div>
    </div>
  );
}
