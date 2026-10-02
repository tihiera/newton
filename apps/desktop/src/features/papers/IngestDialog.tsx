// Add one arXiv paper: agentd fetches it, reads it with the model and writes a card.

import { useState, type FormEvent } from "react";
import { api } from "../../api";
import { useGoals, useProfile } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, Modal, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import "./papers.css";

const ingest = (body: { ref: string; goal_id: string | null; model: string | null }) => api.research.ingest(body);

export function IngestDialog({ goalId, onClose }: { goalId?: string | null; onClose: () => void }) {
  const nav = useNav();
  const goals = useGoals();
  const profile = useProfile();
  const [ref, setRef] = useState("");
  const [goal, setGoal] = useState(goalId ?? "");
  const [model, setModel] = useState("");
  const action = useAction(ingest);
  const err = action.error;
  const selectable = (goals.data ?? []).filter((g) => g.status !== "archived" || g.id === goalId);
  const defaultModel = profile.data?.default_model;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ref.trim() || action.busy) return;
    const item = await action.run({ ref: ref.trim(), goal_id: goal || null, model: model.trim() || null });
    if (!item) return;
    // agentd returns the existing item when the paper is already known: go where it lives.
    if (item.goal_id) nav.showGoal(item.goal_id);
    else nav.showPapers();
    nav.selectPaper(item.id);
    onClose();
  };

  return (
    <Modal onClose={onClose} label="Add arXiv paper">
      <form onSubmit={submit}>
        <div className="modal-body mesh-header stack" style={{ gap: 18 }}>
          <span className="icon-tile round">
            <Icon name="paper" size={24} />
          </span>
          <div>
            <h2 className="h-display">Add a paper</h2>
            <p className="subtitle">Paste an arXiv id or link. Newton reads the paper and writes a research card.</p>
          </div>

          <Field label="arXiv id or URL" error={fieldError(err, "ref")}>
            <input
              className={`input ${fieldError(err, "ref") ? "invalid" : ""}`}
              autoFocus
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="2401.12345 or https://arxiv.org/abs/2401.12345"
              maxLength={300}
            />
          </Field>

          <Field label="Research" error={fieldError(err, "goal_id")} hint="Optional. The paper shows in that research's inbox.">
            <select className="select" value={goal} onChange={(e) => setGoal(e.target.value)}>
              <option value="">No research (library only)</option>
              {selectable.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.title}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="Model"
            error={fieldError(err, "model")}
            hint={defaultModel ? `Optional. Default: ${defaultModel}` : "Optional. No default model is set in Settings."}
          >
            <input
              className={`input ${fieldError(err, "model") ? "invalid" : ""}`}
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={defaultModel ?? "model name"}
              maxLength={256}
            />
          </Field>

          {err ? <ErrorNote error={err} /> : null}
        </div>
        <div className="modal-foot">
          <span className="small muted spacer">PDF upload isn't supported: arXiv papers only.</span>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={!ref.trim() || action.busy}>
            {action.busy ? <Spinner /> : <Icon name="plus" size={16} />}
            Add paper
          </button>
        </div>
      </form>
    </Modal>
  );
}
