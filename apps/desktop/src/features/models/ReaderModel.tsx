// The reader model (what reads papers, and what "default" means in the router), with
// "Change" to pick another one.

import { useState } from "react";
import { api, type Host, type Profile, type RouterStatus, type Service } from "../../api";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { Chip, ErrorNote, fieldError, Spinner, Thumb } from "../../components/ui";
import { DefaultModelPicker } from "./DefaultModelPicker";
import { readerLine } from "./readerView";

export function ReaderModel({ profile, router, services, hosts, onSaved }: {
  profile: Profile | undefined;
  router: RouterStatus | undefined;
  services: Service[] | undefined;
  hosts: Host[] | undefined;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [model, setModel] = useState("");
  const save = useAction(async (m: string) => {
    await api.profile.update({ default_model: m || null });
    return true;
  });
  const line = readerLine(profile, router, services);
  const hostName = line.hostId ? hosts?.find((h) => h.id === line.hostId)?.name ?? line.hostId : null;

  return (
    <div className="card reader-card-lg">
      <div className="row" style={{ gap: 18, flexWrap: "nowrap" }}>
        <Thumb seed={line.model ?? "reader"} size={60} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-card">Reader{line.model ? <> · {line.model}</> : null}</div>
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
        {!editing ? (
          <button
            className="btn"
            disabled={!profile}
            onClick={() => {
              setModel(profile?.default_model ?? "");
              setEditing(true);
            }}
          >
            Change
          </button>
        ) : null}
      </div>
      <div className="small muted" style={{ marginTop: 12 }}>
        The model Newton uses to read papers, and what <span className="mono">default</span> means in the router.
      </div>
      {editing ? (
        <div className="stack" style={{ marginTop: 14, gap: 10 }}>
          <DefaultModelPicker value={model} onChange={setModel} invalid={Boolean(fieldError(save.error, "default_model"))} />
          {save.error ? <ErrorNote error={fieldError(save.error, "default_model") ?? save.error} /> : null}
          <div className="row">
            <button
              className="btn primary"
              disabled={save.busy}
              onClick={async () => {
                if (await save.run(model.trim())) {
                  setEditing(false);
                  onSaved();
                }
              }}
            >
              {save.busy ? <Spinner /> : null}
              Save
            </button>
            <button className="btn ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
