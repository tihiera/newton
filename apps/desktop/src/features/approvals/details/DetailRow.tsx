import type { ReactNode } from "react";
import { Icon } from "../../../components/Icon";

/** One row of an approval card (mockup 03): icon tile, title, lines. */
export function DetailRow({ icon, tile = "", title, children, aside }: {
  icon: string;
  tile?: "" | "blush" | "lavender" | "powder" | "mint";
  title: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="detail-row ap-row">
      <span className={`icon-tile ${tile}`}>
        <Icon name={icon} size={22} />
      </span>
      <div style={{ minWidth: 0 }}>
        <div className="detail-title">{title}</div>
        <div className="detail-lines">{children}</div>
      </div>
      {aside ?? <span />}
    </div>
  );
}

/** label: value pairs inside a detail row. */
export function Pairs({ rows }: { rows: Array<[string, ReactNode]> }) {
  return (
    <div className="ap-pairs">
      {rows.map(([k, v], i) => (
        <div key={`${k}-${i}`} style={{ display: "contents" }}>
          <span>{k}</span>
          <span className="ap-pair-val">{v}</span>
        </div>
      ))}
    </div>
  );
}
