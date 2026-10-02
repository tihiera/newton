// The "Models" drawer, kept simple: the reader model and the model services (running
// ones first; stopped ones behind a toggle).

import { useState } from "react";
import { useHosts, useProfile, useRouterStatus, useServices } from "../../app/data";
import { Icon } from "../../components/Icon";
import { Drawer, Empty, ErrorNote, Spinner } from "../../components/ui";
import { NewServiceForm } from "./NewServiceForm";
import { ReaderModel } from "./ReaderModel";
import { ENDED_SERVICE, ServiceCard } from "./ServiceCard";
import "./models.css";

export function ModelsDrawer({ onClose }: { onClose: () => void }) {
  const profile = useProfile();
  const services = useServices();
  const router = useRouterStatus();
  const hosts = useHosts();
  const [adding, setAdding] = useState(false);
  // The reader's "Start it": the form opens with that model picked.
  const [startModel, setStartModel] = useState<string | undefined>(undefined);
  const [showStopped, setShowStopped] = useState(false);
  const all = services.data ?? [];
  const running = all.filter((s) => !ENDED_SERVICE.has(s.state));
  const stopped = all.filter((s) => ENDED_SERVICE.has(s.state));

  return (
    <Drawer
      title="Models"
      onClose={onClose}
      footer={
        <button className="btn primary large block" onClick={onClose}>
          Done
        </button>
      }
    >
      <div className="models">
        <h3 className="h-section models-heading">Reader</h3>
        {profile.error && !profile.data ? <ErrorNote error={profile.error} /> : null}
        <ReaderModel
          profile={profile.data}
          router={router.data}
          services={services.data}
          hosts={hosts.data}
          onSaved={profile.refresh}
          onStart={(model) => {
            setStartModel(model);
            setAdding(true);
          }}
        />

        <div className="models-section-head">
          <h3 className="h-section models-heading">Services</h3>
          {!adding ? (
            <button
              className="btn"
              onClick={() => {
                setStartModel(undefined);
                setAdding(true);
              }}
            >
              <Icon name="plus" size={17} /> New service
            </button>
          ) : null}
        </div>
        {adding ? (
          <NewServiceForm
            key={startModel ?? "new"}
            hosts={hosts.data}
            onCreated={services.refresh}
            onClose={() => setAdding(false)}
            initialModel={startModel}
          />
        ) : null}
        {services.error && !services.data ? <ErrorNote error={services.error} /> : null}
        {!services.data && services.loading ? (
          <div className="row small muted">
            <Spinner /> Loading services…
          </div>
        ) : null}
        {services.data && running.length === 0 && !adding ? (
          <Empty title="No model running" icon="model">
            Start one with New service.
          </Empty>
        ) : null}
        <div className="stack" style={{ gap: 10 }}>
          {running.map((s) => (
            <ServiceCard key={s.id} service={s} hosts={hosts.data} refresh={services.refresh} />
          ))}
        </div>
        {stopped.length ? (
          <>
            <button className="btn ghost small-link models-stopped-toggle" onClick={() => setShowStopped(!showStopped)}>
              <Icon name={showStopped ? "chevronDown" : "chevronRight"} size={14} />
              Stopped ({stopped.length})
            </button>
            {showStopped ? (
              <div className="stack" style={{ gap: 10 }}>
                {stopped.map((s) => (
                  <ServiceCard key={s.id} service={s} hosts={hosts.data} refresh={services.refresh} />
                ))}
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </Drawer>
  );
}
