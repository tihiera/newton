import { useEffect, useRef, useState } from "react";
import type { GoalStatus } from "../../api";
import { Icon } from "../../components/Icon";
import { Spinner } from "../../components/ui";

const ACTIONS: Record<GoalStatus, Array<{ to: GoalStatus; label: string; icon: string }>> = {
  active: [
    { to: "paused", label: "Pause watching", icon: "pause" },
    { to: "archived", label: "Archive", icon: "archive" },
  ],
  paused: [
    { to: "active", label: "Resume watching", icon: "play" },
    { to: "archived", label: "Archive", icon: "archive" },
  ],
  archived: [{ to: "active", label: "Restore", icon: "refresh" }],
};

/** Pause / resume / archive, in a small menu next to the status. */
export function StatusMenu({
  status,
  busy,
  onChange,
}: {
  status: GoalStatus;
  busy: boolean;
  onChange: (status: GoalStatus) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="goal-menu" ref={ref}>
      <button
        className="icon-btn outlined"
        style={{ width: 44, height: 44 }}
        onClick={() => setOpen(!open)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Research options"
        title="Research options"
        disabled={busy}
      >
        {busy ? <Spinner /> : <Icon name="more" />}
      </button>
      {open ? (
        <div className="goal-menu-list" role="menu">
          {(ACTIONS[status] ?? ACTIONS.active).map((a) => (
            <button
              key={a.to}
              role="menuitem"
              className="goal-menu-item"
              onClick={() => {
                setOpen(false);
                onChange(a.to);
              }}
            >
              <Icon name={a.icon} size={17} />
              {a.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
