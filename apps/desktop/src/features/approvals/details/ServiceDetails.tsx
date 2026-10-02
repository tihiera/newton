// start_service details: where, what, how much it downloads, and the host's
// admission numbers, all as agentd sent them.

import { Note } from "../../../components/ui";
import { num } from "../../../components/time";
import { humanKey, numOrUndef, obj, plain, str } from "../text";
import { DetailRow, Pairs } from "./DetailRow";

export function ServiceDetails({ details }: { details: Record<string, unknown> }) {
  const host = obj(details.host);
  const download = details.download === true;
  const dlGb = numOrUndef(details.download_gb_estimate);
  const memory = numOrUndef(details.memory_gb);
  const admission = obj(details.admission);
  const extras: Array<[string, string]> = [];
  if (details.context_length !== undefined) extras.push(["Context length:", plain(details.context_length)]);
  if (details.parallel !== undefined) extras.push(["Parallel requests:", plain(details.parallel)]);

  return (
    <div>
      {details.trust_remote_code === true ? (
        <div style={{ marginBottom: 10 }}>
          <Note tone="warn" icon="alert">
            This model runs code shipped with it (trust_remote_code). Approve only if you trust its source.
          </Note>
        </div>
      ) : null}
      <DetailRow icon="server" title="Host">
        <div className="ap-strong">{str(host?.name) ?? plain(details.host)}</div>
      </DetailRow>
      <DetailRow icon="model" tile="lavender" title="Model">
        <div className="ap-strong">{str(details.model) ?? "—"}</div>
        <Pairs
          rows={[
            ["Engine:", str(details.engine) ?? "—"],
            ["Revision:", <span className="mono">{str(details.revision) ?? "not pinned"}</span>],
          ]}
        />
      </DetailRow>
      <DetailRow icon="database" tile="blush" title="Download">
        <div className="ap-strong">
          {download ? `Downloads the model${dlGb !== undefined ? ` (up to ${num(dlGb)} GB)` : ""}` : "Already on the host: no download"}
        </div>
      </DetailRow>
      <DetailRow icon="chip" tile="powder" title="Memory">
        <div className="ap-strong">{memory !== undefined ? `${num(memory)} GB` : "—"}</div>
        {extras.length ? <Pairs rows={extras} /> : null}
      </DetailRow>
      {admission ? (
        <DetailRow icon="shield" tile="mint" title="Admission">
          <Pairs rows={Object.entries(admission).map(([k, v]) => [`${humanKey(k)}:`, plain(v)])} />
        </DetailRow>
      ) : null}
    </div>
  );
}
