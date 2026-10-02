import type { ResearchItem } from "../../api";
import { StateChip, Thumb } from "../../components/ui";
import { paperMeta, paperTitle } from "./format";

/** One paper in the inbox: thumbnail, title (two lines), meta, state. */
export function PaperRow({ item, selected, onSelect }: {
  item: ResearchItem;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button
      className={`paper-row ${selected ? "selected mesh-selected" : ""}`}
      onClick={() => onSelect(item.id)}
      aria-current={selected ? "true" : undefined}
      title={paperTitle(item)}
    >
      <Thumb seed={item.id} />
      <div className="paper-row-body">
        <div className="paper-row-title">{paperTitle(item)}</div>
        <div className="paper-row-meta">{paperMeta(item)}</div>
        <StateChip kind="paper" state={item.state} />
      </div>
    </button>
  );
}
