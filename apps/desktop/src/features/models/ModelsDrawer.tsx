// The "Models" drawer: the reader model, the model services on each machine, the
// router (what is in flight, what a timed run has paused) and its request log.

import { useState } from "react";
import { useHosts, useProfile, useRouterStatus, useServices } from "../../app/data";
import { Icon } from "../../components/Icon";
import { Drawer, Empty, ErrorNote, Spinner } from "../../components/ui";
import { NewServiceForm } from "./NewServiceForm";
import { ReaderModel } from "./ReaderModel";
import { RequestLog } from "./RequestLog";
import { RouterPanel } from "./RouterPanel";
import { ServiceCard } from "./ServiceCard";
import "./models.css";

export function ModelsDrawer({ onClose }: { onClose: () => void }) {
  const profile = useProfile();
  const services = useServices();
  const router = useRouterStatus();
  const hosts = useHosts();
  const [adding, setAdding] = useState(false);
  // The reader's "Start it": the form opens with that model picked.
  const [startModel, setStartModel] = useState<string | undefined>(undefined);
  const list = services.data ?? [];

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
        {services.data && list.length === 0 && !adding ? (
          <Empty title="No model services" icon="model">
            Serve a model on a machine to read papers with it. Everything goes through Newton's router.
          </Empty>
        ) : null}
        <div className="stack" style={{ gap: 14 }}>
          {list.map((s) => (
            <ServiceCard
              key={s.id}
              service={s}
              hosts={hosts.data}
              routes={router.data?.models}
              refresh={services.refresh}
            />
          ))}
        </div>

        <h3 className="h-section models-heading">Router</h3>
        <RouterPanel status={router.data} error={router.error} hosts={hosts.data} />

        <h3 className="h-section models-heading">Recent requests</h3>
        <RequestLog hosts={hosts.data} />
      </div>
    </Drawer>
  );
}
