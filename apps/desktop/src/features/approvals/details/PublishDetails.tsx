// publish_report details (mockup 07): destination facts, then the preview of exactly
// what will be sent, with its hash.

import type { Evidence } from "../../../api";
import { Icon } from "../../../components/Icon";
import { EvidenceBadge } from "../../../components/ui";
import { Markdown } from "../../../components/Markdown";
import { destinationFacts, numOrUndef, shortHash, str } from "../text";

const EVIDENCE = new Set(["green", "yellow", "red", "unknown"]);

export function PublishDetails({ details }: { details: Record<string, unknown> }) {
  const facts = destinationFacts(details.target, details.destination);
  const chars = numOrUndef(details.characters);
  const preview = str(details.preview) ?? "";
  const evidence = EVIDENCE.has(String(details.evidence)) ? (details.evidence as Evidence) : "unknown";

  return (
    <div className="stack">
      <div className="facts">
        <div className="fact">
          <div className="fact-label">Destination</div>
          <div className="fact-value">
            <Icon name={facts.icon} size={20} />
            {facts.destination}
          </div>
        </div>
        {facts.whereLabel ? (
          <div className="fact">
            <div className="fact-label">{facts.whereLabel}</div>
            <div className="fact-value mono">{facts.where ?? "—"}</div>
          </div>
        ) : null}
        <div className="fact">
          <div className="fact-label">Evidence</div>
          <div className="fact-value">
            <EvidenceBadge evidence={evidence} />
          </div>
        </div>
        <div className="fact">
          <div className="fact-label">Length</div>
          <div className="fact-value">
            <Icon name="list" size={20} />
            {chars !== undefined ? `${chars.toLocaleString()} characters` : "—"}
          </div>
        </div>
      </div>

      <div className="preview mesh-card ap-preview">
        {preview ? <Markdown text={preview} /> : <div className="muted">No preview.</div>}
        <div className="ap-preview-foot">
          <Icon name="shield" size={16} />
          Preview hash<span className="ap-sep">·</span>
          <span className="mono">{shortHash(details.sha256)}</span>
          {chars !== undefined && preview && chars > preview.length ? (
            <span className="spacer" style={{ textAlign: "right" }}>
              First {preview.length.toLocaleString()} of {chars.toLocaleString()} characters
            </span>
          ) : null}
        </div>
      </div>
      {str(details.experiment_id) ? (
        <div className="small muted">
          Report of experiment <span className="mono">{str(details.experiment_id)}</span>
        </div>
      ) : null}
    </div>
  );
}
