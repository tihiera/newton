// The "Models" section at the bottom of the compute drawer: the reader model, where
// it runs and whether it is ready, with a way to the models drawer.

import type { Host } from "../../api";
import { useProfile, useRouterStatus, useServices } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { Chip, Thumb } from "../../components/ui";
import { readerLine } from "../models/readerView";

export function ReaderSummary({ hosts }: { hosts: Host[] | undefined }) {
  const nav = useNav();
  const profile = useProfile();
  const router = useRouterStatus();
  const services = useServices();
  const line = readerLine(profile.data, router.data, services.data);
  const hostName = line.hostId ? (hosts?.find((h) => h.id === line.hostId)?.name ?? line.hostId) : null;

  return (
    <div className="card reader-card">
      <Thumb seed={line.model ?? "reader"} size={60} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="h-card reader-title">
          Reader
          {line.model ? (
            <>
              {" "}
              · <span className="reader-model">{line.model}</span>
            </>
          ) : null}
        </div>
        <div className="row" style={{ marginTop: 6, gap: 12 }}>
          {hostName ? (
            <span className="row small muted" style={{ gap: 6 }}>
              <Icon name="server" size={16} /> {hostName}
            </span>
          ) : null}
          <Chip tone={line.tone}>
            <span className={`dot ${line.dot}`} />
            {line.label}
          </Chip>
        </div>
      </div>
      <button className="btn" onClick={() => nav.open({ kind: "models" })}>
        {line.model ? "Change" : "Choose"}
      </button>
    </div>
  );
}
