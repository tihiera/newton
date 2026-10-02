import type { ResearchItem } from "../../api";
import { Icon } from "../../components/Icon";
import { Chip } from "../../components/ui";
import { correctionText, timeMethodText, yesNo } from "./format";

/** Document -> graph: the paper's method turned into Newton's scheme IR. */
function SchemeVisual() {
  return (
    <div className="scheme-visual" aria-hidden>
      <div className="scheme-doc">
        <span />
        <span />
        <span />
      </div>
      <Icon name="chevronRight" size={26} className="muted" />
      <svg width="120" height="104" viewBox="0 0 120 104">
        <g stroke="rgba(30,24,16,0.35)" strokeWidth="1.4">
          <line x1="60" y1="16" x2="22" y2="52" />
          <line x1="60" y1="16" x2="98" y2="52" />
          <line x1="22" y1="52" x2="60" y2="88" />
          <line x1="98" y1="52" x2="60" y2="88" />
        </g>
        <circle cx="60" cy="16" r="13" fill="#f9d96b" />
        <circle cx="22" cy="52" r="13" fill="#f3a3c8" />
        <circle cx="98" cy="52" r="13" fill="#c9b6f2" />
        <circle cx="60" cy="88" r="13" fill="#a9c4f5" />
      </svg>
    </div>
  );
}

/** Whether the card maps onto a testable scheme, and the IR if it does. */
export function SchemeMapping({ item }: { item: ResearchItem }) {
  const ir = item.data.scheme_ir;
  const note = item.data.scheme_note;
  return (
    <section className="card">
      <div className="card-head">
        <span className="icon-btn outlined" style={{ width: 42, height: 42 }}>
          <Icon name="graph" />
        </span>
        <h3 className="h-card spacer">Scheme mapping</h3>
      </div>
      {ir ? (
        <>
          <div className="row" style={{ justifyContent: "flex-end", marginTop: -6 }}>
            <Chip tone="green">
              <span className="dot ok" />
              Mapped onto Newton's IR
            </Chip>
          </div>
          <SchemeVisual />
          <div className="pw-prov-grid" style={{ marginTop: 0 }}>
            <span className="k">IR name</span>
            <span className="v mono">{ir.name}</span>
            <span className="k">Limiter</span>
            <span className="v">{ir.flux.limiter.replace(/_/g, " ")}</span>
            <span className="k">Correction A(c)</span>
            <span className="v mono">{correctionText(ir.flux.correction)}</span>
            <span className="k">Time</span>
            <span className="v">{timeMethodText(ir.time)}</span>
            <span className="k">Claims</span>
            <span className="v">
              order {ir.claims.order} · max CFL {ir.claims.max_cfl} · TVD {yesNo(ir.claims.tvd).toLowerCase()}
            </span>
            {item.data.scheme_ir_digest ? (
              <>
                <span className="k">Digest</span>
                <span className="v mono" title={item.data.scheme_ir_digest}>
                  {item.data.scheme_ir_digest.slice(0, 16)}…
                </span>
              </>
            ) : null}
          </div>
          {note ? (
            <p className="small muted" style={{ marginTop: 12 }}>
              {note}
            </p>
          ) : null}
        </>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          <div>
            <Chip tone="gray">Not mapped</Chip>
          </div>
          {note ? <p className="pw-body">{note}</p> : <p className="pw-body">No mapping yet.</p>}
        </div>
      )}
    </section>
  );
}
