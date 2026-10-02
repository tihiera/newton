import { useHosts, useGoals, useProfile, useRouterStatus, useServices } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { computeStatus, readerStatus } from "./status";

export function Sidebar() {
  const nav = useNav();
  const goals = useGoals();
  const hosts = useHosts();
  const profile = useProfile();
  const services = useServices();
  const router = useRouterStatus();
  const compute = computeStatus(hosts.data);
  const reader = readerStatus(profile.data, services.data, router.data);
  const visibleGoals = (goals.data ?? []).filter((g) => g.status !== "archived");
  const archived = (goals.data ?? []).filter((g) => g.status === "archived");

  return (
    <nav className="panel sidebar" aria-label="Newton">
      <div className="brand">
        <span className="logo-mark" />
        <span className="brand-name">Newton</span>
      </div>

      <button
        className={`nav-item ${nav.goalId === null ? "selected mesh-selected" : ""}`}
        onClick={nav.showPapers}
        aria-current={nav.goalId === null ? "page" : undefined}
      >
        <Icon name="paper" />
        <span className="nav-label">Papers</span>
      </button>

      <div className="nav-divider" />
      <div className="nav-section">Researches</div>
      <button className="nav-item" onClick={() => nav.open({ kind: "new-research" })}>
        <span className="plus-dot">
          <Icon name="plus" size={15} />
        </span>
        <span className="nav-label">New research</span>
      </button>

      <div className="nav-scroll">
        {visibleGoals.map((g) => (
          <button
            key={g.id}
            className={`nav-item ${nav.goalId === g.id ? "selected mesh-selected" : ""}`}
            onClick={() => nav.showGoal(g.id)}
            title={g.title}
            aria-current={nav.goalId === g.id ? "page" : undefined}
          >
            <Icon name="folder" />
            <span className="nav-label">{g.title}</span>
            {g.status === "paused" ? <span className="muted-count">paused</span> : null}
          </button>
        ))}
        {archived.length ? (
          <details>
            <summary className="nav-section" style={{ cursor: "pointer" }}>
              Archived ({archived.length})
            </summary>
            {archived.map((g) => (
              <button
                key={g.id}
                className={`nav-item ${nav.goalId === g.id ? "selected mesh-selected" : ""}`}
                onClick={() => nav.showGoal(g.id)}
              >
                <Icon name="archive" />
                <span className="nav-label">{g.title}</span>
              </button>
            ))}
          </details>
        ) : null}
        {goals.error && !goals.data ? <div className="small muted" style={{ padding: "6px 12px" }}>{goals.error.message}</div> : null}
      </div>

      <div className="sidebar-foot">
        <button className="status-row" onClick={() => nav.open({ kind: "compute" })} title="Compute">
          <span className={`dot ${compute.dot}`} />
          <span className="grow">{compute.label}</span>
          <Icon name="chevronRight" size={15} />
        </button>
        <button className="status-row" onClick={() => nav.open({ kind: "models" })} title="Models">
          <span className={`dot ${reader.dot}`} />
          <span className="grow">{reader.label}</span>
          <Icon name="chevronRight" size={15} />
        </button>
        <div className="profile-row">
          <span className="avatar" />
          <span style={{ flex: 1, fontWeight: 500 }}>{profile.data?.display_name || "You"}</span>
          <button className="icon-btn" onClick={() => nav.open({ kind: "settings" })} aria-label="Settings">
            <Icon name="settings" />
          </button>
        </div>
      </div>
    </nav>
  );
}
