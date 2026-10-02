// Create a research goal: what to look for on arXiv and how often.

import { useState, type FormEvent } from "react";
import { api, type GoalCreate } from "../../api";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Modal, Spinner, Switch } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import "./goals.css";

const DEFAULT_CATEGORIES = "physics.comp-ph, math.NA, physics.flu-dyn";

/** "a, b,, c" -> ["a", "b", "c"] */
function splitList(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

const create = (body: GoalCreate) => api.goals.create(body);

export function NewResearchDialog({ onClose }: { onClose: () => void }) {
  const nav = useNav();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [keywords, setKeywords] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [categories, setCategories] = useState(DEFAULT_CATEGORIES);
  const [pollHours, setPollHours] = useState("24");
  const [autoPropose, setAutoPropose] = useState(true);
  const action = useAction(create);
  const err = action.error;
  const fe = (name: string) => fieldError(err, name);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim() || action.busy) return;
    const goal = await action.run({
      title: title.trim(),
      description: description.trim(),
      keywords: splitList(keywords),
      categories: splitList(categories),
      poll_hours: pollHours.trim() ? Number(pollHours) : undefined,
      auto_propose: autoPropose,
    });
    if (!goal) return;
    nav.showGoal(goal.id);
    onClose();
  };

  // A 422 on a field under Advanced keeps that section open so the message shows.
  const advancedError = Boolean(fe("categories") || fe("poll_hours") || fe("auto_propose"));

  return (
    <Modal onClose={onClose} label="New research">
      <form onSubmit={submit}>
        <div className="modal-body mesh-header stack" style={{ gap: 18 }}>
          <span className="icon-tile round">
            <Icon name="folder" size={24} />
          </span>
          <div>
            <h2 className="h-display">New research</h2>
            <p className="subtitle">
              Newton watches arXiv for this, reads what's relevant, and proposes experiments. Nothing runs without your
              approval.
            </p>
          </div>

          <Field label="Title" error={fe("title")}>
            <input
              className={`input ${fe("title") ? "invalid" : ""}`}
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Higher-order advection schemes that beat upwind"
              maxLength={200}
            />
          </Field>

          <Field label="Description" error={fe("description")} hint="What you want to find out. The model uses it to triage papers.">
            <textarea
              className={`textarea ${fe("description") ? "invalid" : ""}`}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={10000}
            />
          </Field>

          <Field label="Keywords" error={fe("keywords")} hint="Comma separated, e.g. flux limiter, TVD scheme">
            <input
              className={`input ${fe("keywords") ? "invalid" : ""}`}
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              placeholder="flux limiter, TVD scheme"
            />
          </Field>

          <div>
            <button
              type="button"
              className={`advanced-toggle ${advanced || advancedError ? "open" : ""}`}
              onClick={() => setAdvanced(!advanced)}
              aria-expanded={advanced || advancedError}
            >
              Advanced
              <Icon name="chevronDown" size={16} />
            </button>
          </div>

          {advanced || advancedError ? (
            <div className="stack" style={{ gap: 16 }}>
              <Field label="arXiv categories" error={fe("categories")} hint="Comma separated">
                <input
                  className={`input ${fe("categories") ? "invalid" : ""}`}
                  value={categories}
                  onChange={(e) => setCategories(e.target.value)}
                />
              </Field>
              <Field label="Look every (hours)" error={fe("poll_hours")}>
                <input
                  className={`input ${fe("poll_hours") ? "invalid" : ""}`}
                  type="number"
                  min={1}
                  max={720}
                  value={pollHours}
                  onChange={(e) => setPollHours(e.target.value)}
                  style={{ maxWidth: 160 }}
                />
              </Field>
              <div className="switch-row">
                <Switch on={autoPropose} onChange={setAutoPropose} label="Propose experiments automatically" />
                <div>
                  <div>Propose experiments automatically</div>
                  <div className="small muted">They still wait for your approval before anything runs.</div>
                </div>
              </div>
            </div>
          ) : null}

          {err ? <ErrorNote error={err} /> : null}
        </div>
        <div className="modal-foot">
          <span className="spacer" />
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={!title.trim() || action.busy}>
            {action.busy ? <Spinner /> : <Icon name="plus" size={16} />}
            Create research
          </button>
        </div>
      </form>
    </Modal>
  );
}
