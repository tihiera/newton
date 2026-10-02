// The local profile: the name Newton shows, the reader model, and the Mac models
// switch. Only changed fields are sent (PATCH /profile).

import { useState } from "react";
import { api, type Profile } from "../../api";
import { useAction } from "../../hooks/useAction";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Spinner, Switch } from "../../components/ui";
import { DefaultModelPicker } from "../models/DefaultModelPicker";

export function ProfileSection({ profile, refresh }: { profile: Profile | undefined; refresh: () => void }) {
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [saved, setSaved] = useState(false);

  // The form starts from the profile once it has arrived (then it's the user's).
  if (profile && !loaded) {
    setName(profile.display_name ?? "");
    setModel(profile.default_model ?? "");
    setLoaded(true);
  }

  const changes: { display_name?: string | null; default_model?: string | null } = {};
  if (profile && name.trim() !== (profile.display_name ?? "")) changes.display_name = name.trim() || null;
  if (profile && model.trim() !== (profile.default_model ?? "")) changes.default_model = model.trim() || null;
  const dirty = Object.keys(changes).length > 0;

  const save = useAction(async () => {
    await api.profile.update(changes);
    setSaved(true);
    refresh();
  });
  const fe = (n: string) => fieldError(save.error, n);
  const unmapped = save.error && !fe("display_name") && !fe("default_model");

  return (
    <section className="settings-card card">
      <div className="settings-grid">
        <Field label="Your name" error={fe("display_name")}>
          <input
            className={`input ${fe("display_name") ? "invalid" : ""}`}
            value={name}
            disabled={!profile}
            onChange={(e) => {
              setName(e.target.value);
              setSaved(false);
            }}
            placeholder="How Newton greets you"
          />
        </Field>
        <Field
          label="Reader model"
          error={fe("default_model")}
          hint="Reads papers, and is what “default” means in the router."
        >
          <DefaultModelPicker
            value={model}
            onChange={(m) => {
              setModel(m);
              setSaved(false);
            }}
            invalid={Boolean(fe("default_model"))}
          />
        </Field>
      </div>
      {unmapped ? <ErrorNote error={save.error} /> : null}
      <div className="row">
        <button className="btn primary" disabled={!dirty || save.busy} onClick={() => save.run()}>
          {save.busy ? <Spinner /> : null}
          Save profile
        </button>
        {saved && !dirty ? <span className="small muted">Saved.</span> : null}
      </div>
    </section>
  );
}

export function MacModelsSection({ profile, refresh }: { profile: Profile | undefined; refresh: () => void }) {
  const toggle = useAction(async (on: boolean) => {
    await api.profile.update({ mac_models: on });
    refresh();
  });
  return (
    <section className="settings-card card">
      <div className="settings-switch-row">
        <span className="icon-tile powder" style={{ width: 44, height: 44, borderRadius: 14 }}>
          {toggle.busy ? <Spinner /> : <Icon name="laptop" size={22} />}
        </span>
        <div style={{ flex: 1 }}>
          <div className="h-card">Mac models</div>
          <div className="small muted">Small MLX models on this Mac, only on AC power. Off stops them.</div>
        </div>
        <Switch
          label="Mac models"
          on={Boolean(profile?.mac_models)}
          disabled={!profile || toggle.busy}
          onChange={(next) => toggle.run(next)}
        />
      </div>
      {toggle.error ? <ErrorNote error={toggle.error} /> : null}
    </section>
  );
}
