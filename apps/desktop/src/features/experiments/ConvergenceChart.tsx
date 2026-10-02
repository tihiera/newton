// Log-log L2 error vs grid spacing, one line per variant: a plain drawing of the
// numbers the benchmark returned (no fits, no derived values).
// Colours: baseline neutral (dashed), candidates in a fixed, CVD-checked order.

import { useMemo, useState } from "react";
import { num } from "../../components/time";
import { decades, type Series } from "./view";

const CANDIDATE_COLORS = ["#2f6fe0", "#c2408a", "#8a5d00", "#1f9a8a"];
const BASELINE_COLOR = "#8b857c";

const W = 640;
const H = 270;
const M = { top: 16, right: 18, bottom: 46, left: 62 };
const PW = W - M.left - M.right;
const PH = H - M.top - M.bottom;

function Pow({ e }: { e: number }) {
  return (
    <>
      10
      <tspan dy={-6} fontSize={9}>
        {e}
      </tspan>
    </>
  );
}

export function ConvergenceChart({ series }: { series: Series[] }) {
  const [hover, setHover] = useState<{ s: number; p: number } | null>(null);

  const { xd, yd, x, y, colors } = useMemo(() => {
    const all = series.flatMap((s) => s.points);
    const xd = decades(Math.min(...all.map((p) => p.dx)), Math.max(...all.map((p) => p.dx)));
    const yd = decades(Math.min(...all.map((p) => p.l2)), Math.max(...all.map((p) => p.l2)));
    const [x0, x1] = [xd[0], xd[xd.length - 1]];
    const [y0, y1] = [yd[0], yd[yd.length - 1]];
    const x = (v: number) => M.left + ((Math.log10(v) - x0) / (x1 - x0)) * PW;
    const y = (v: number) => M.top + PH - ((Math.log10(v) - y0) / (y1 - y0)) * PH;
    let c = 0;
    const colors = series.map((s) =>
      s.role === "baseline" ? BASELINE_COLOR : CANDIDATE_COLORS[c++ % CANDIDATE_COLORS.length],
    );
    return { xd, yd, x, y, colors };
  }, [series]);

  const hp = hover ? series[hover.s]?.points[hover.p] : undefined;

  return (
    <div className="ex-chart">
      <div className="row ex-chart-head">
        <div className="h-card">Convergence (L2 error)</div>
        <span className="spacer" />
        <div className="ex-legend">
          {series.map((s, i) => (
            <span key={s.jobId} className="ex-legend-item">
              <svg width="22" height="10" aria-hidden>
                <line
                  x1="1"
                  y1="5"
                  x2="21"
                  y2="5"
                  stroke={colors[i]}
                  strokeWidth="2"
                  strokeDasharray={s.role === "baseline" ? "4 3" : undefined}
                />
                <circle cx="11" cy="5" r="3.5" fill={colors[i]} />
              </svg>
              {s.label}
            </span>
          ))}
        </div>
      </div>
      <div className="ex-chart-box">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label="L2 error against grid spacing, log-log"
          onMouseLeave={() => setHover(null)}
        >
          {yd.map((e) => (
            <g key={`y${e}`}>
              <line x1={M.left} x2={W - M.right} y1={y(10 ** e)} y2={y(10 ** e)} className="ex-grid" />
              <text x={M.left - 10} y={y(10 ** e) + 4} textAnchor="end" className="ex-tick">
                <Pow e={e} />
              </text>
            </g>
          ))}
          {xd.map((e) => (
            <g key={`x${e}`}>
              <line x1={x(10 ** e)} x2={x(10 ** e)} y1={M.top} y2={M.top + PH} className="ex-grid faint" />
              <text x={x(10 ** e)} y={M.top + PH + 20} textAnchor="middle" className="ex-tick">
                <Pow e={e} />
              </text>
            </g>
          ))}
          <line x1={M.left} x2={W - M.right} y1={M.top + PH} y2={M.top + PH} className="ex-axis" />
          <text x={M.left + PW / 2} y={H - 6} textAnchor="middle" className="ex-axis-label">
            Grid spacing dx
          </text>
          <text transform={`translate(16 ${M.top + PH / 2}) rotate(-90)`} textAnchor="middle" className="ex-axis-label">
            L2 error
          </text>

          {series.map((s, si) => (
            <g key={s.jobId}>
              <polyline
                points={s.points.map((p) => `${x(p.dx)},${y(p.l2)}`).join(" ")}
                fill="none"
                stroke={colors[si]}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeDasharray={s.role === "baseline" ? "6 4" : undefined}
              />
              {s.points.map((p, pi) => {
                const on = hover?.s === si && hover.p === pi;
                return (
                  <g key={pi} onMouseEnter={() => setHover({ s: si, p: pi })}>
                    <circle cx={x(p.dx)} cy={y(p.l2)} r={12} fill="transparent" />
                    <circle cx={x(p.dx)} cy={y(p.l2)} r={on ? 6 : 4} fill={colors[si]} stroke="#fff" strokeWidth={2} />
                  </g>
                );
              })}
            </g>
          ))}
        </svg>
        {hover && hp ? (
          <div
            className="ex-tooltip"
            style={{
              left: `${(x(hp.dx) / W) * 100}%`,
              top: `${(y(hp.l2) / H) * 100}%`,
            }}
          >
            <div className="ex-tooltip-title">
              <span className="ex-swatch" style={{ background: colors[hover.s] }} />
              {series[hover.s].label}
            </div>
            <div>nx {hp.nx}</div>
            <div>dx {num(hp.dx)}</div>
            <div>L2 {num(hp.l2)}</div>
          </div>
        ) : null}
      </div>
      <details className="ex-data">
        <summary>Data</summary>
        <table className="ex-table">
          <thead>
            <tr>
              <th>Variant</th>
              <th>nx</th>
              <th>dx</th>
              <th>L2 error</th>
            </tr>
          </thead>
          <tbody>
            {series.flatMap((s) =>
              s.points.map((p) => (
                <tr key={`${s.jobId}-${p.nx}`}>
                  <td>{s.label}</td>
                  <td>{p.nx}</td>
                  <td>{num(p.dx)}</td>
                  <td>{num(p.l2)}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </details>
    </div>
  );
}
