import type { ResearchItem } from "../../api";
import { Empty } from "../../components/ui";

/** What a tab shows before the paper has a research card. */
export function NoCard({ item }: { item: ResearchItem }) {
  return (
    <Empty title="No research card yet" icon="paper">
      {item.state === "failed"
        ? "Newton couldn't read this paper. The Summary tab says why."
        : item.state === "dismissed"
          ? "This paper was dismissed as not relevant. The Summary tab says why."
          : "Newton is still reading this paper. The card appears here when it's done."}
    </Empty>
  );
}
