// "New experiment from built-in schemes": pick schemes from Newton's library to test
// against a baseline (a grid-refinement study, no paper needed). agentd
// builds the experiment; the review dialog then opens on its approval, and nothing
// runs until the user approves there.

import { useCallback, useState, type FormEvent } from "react";
import { api, type LibraryExperimentCreate } from "../../api";
import { useGoals, useHosts } from "../../app/data";
import { useNav } from "../../app/navigation";
import { Icon } from "../../components/Icon";
import { ErrorNote, Field, fieldError, formError, Modal, Spinner } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { usePolling } from "../../hooks/usePolling";
import { BACKENDS, BASELINES, INITIAL } from "./choices";
import {
  canPickCandidate,
  DEFAULT_LIBRARY_CHOICE,
  libraryBody,
  schemeClaims,
  toggleCandidate,
  withBaseline,
  type LibraryChoice,
} from "./library";
import "./experiments.css";

const SHOWN_FIELDS = ["candidates", "baseline", "initial_condition", "host_id", "backend"];

export function LibraryExperimentDialog({ goalId, onClose }: { goalId?: string | null; onClose: () => void }) {
  const nav = useNav();
  const hosts = useHosts();
  const goals = useGoals();
  const schemes = usePolling(() => api.research.schemes(), [], { interval: 0, followEvents: false });
  const [choice, setChoice] = useState<LibraryChoice>(DEFAULT_LIBRARY_CHOICE);
  const set = (patch: Partial<LibraryChoice>) => setChoice((c) => ({ ...c, ...patch }));
  const goal = goalId ? goals.data?.find((g) => g.id === goalId) : undefined;

  const create = useCallback(async (body: LibraryExperimentCreate) => {
    const exp = await api.experiments.fromLibrary(body);
    const approval = exp.approval ?? (await api.experiments.get(exp.id)).approval;
    return { experimentId: exp.id, approvalId: approval?.id ?? null };
  }, []);
  const action = useAction(create);
  const err = action.error;
  const library = schemes.data ?? [];
  const order = library.map((s) => s.name);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!choice.candidates.length || action.busy) return;
    const made = await action.run(libraryBody(choice, goalId));
    if (!made) return;
    // The review replaces this dialog; without an approval to review, show the experiment.
    if (made.approvalId) nav.open({ kind: "review", approvalId: made.approvalId });
    else {
      nav.showExperiment(made.experimentId);
      onClose();
    }
  };

  return (
    <Modal onClose={onClose} label="New experiment from built-in schemes" wide>
      <form onSubmit={submit}>
        <div className="modal-body mesh-header stack" style={{ gap: 18 }}>
          <span className="icon-tile round">
            <Icon name="flask" size={24} />
          </span>
          <div>
            <h2 className="h-display">New experiment</h2>
            <p className="subtitle">
              Test schemes from Newton's built-in library against a baseline. You review the plan before anything runs.
            </p>
            {goal ? <div className="small muted">For {goal.title}</div> : null}
          </div>

          {/* Not a <label>: a click on its text would toggle the first scheme. */}
          <div className="field">
            <span className="field-label">Schemes to test</span>
            {schemes.error && !schemes.data ? (
              <ErrorNote error={schemes.error} />
            ) : !schemes.data ? (
              <div className="row muted">
                <Spinner /> Loading the library…
              </div>
            ) : (
              <div className="lib-schemes" role="group" aria-label="Schemes to test">
                {library.map((s) => {
                  const on = choice.candidates.includes(s.name);
                  const pickable = canPickCandidate(choice, s.name);
                  return (
                    <button
                      type="button"
                      key={s.name}
                      className={`lib-scheme ${on ? "on" : ""}`}
                      aria-pressed={on}
                      disabled={!pickable}
                      title={pickable ? undefined : "This is the baseline"}
                      onClick={() => set({ candidates: toggleCandidate(choice.candidates, s.name, order) })}
                    >
                      <span className="lib-check">{on ? <Icon name="check" size={14} /> : null}</span>
                      <span style={{ minWidth: 0 }}>
                        <span className="mono lib-name">{s.name}</span>
                        <span className="small muted lib-claims">
                          {pickable ? schemeClaims(s) : "The baseline: every candidate is compared with it"}
                        </span>
                        {s.document?.description ? (
                          <span className="small muted lib-desc">{s.document.description}</span>
                        ) : null}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            {fieldError(err, "candidates") ? (
              <span className="field-error">{fieldError(err, "candidates")}</span>
            ) : null}
          </div>

          <div className="ex-form" style={{ margin: 0 }}>
            <Field label="Baseline" error={fieldError(err, "baseline")}>
              <select
                className="select"
                value={choice.baseline}
                onChange={(e) => setChoice((c) => withBaseline(c, e.target.value))}
              >
                {BASELINES.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Initial condition" error={fieldError(err, "initial_condition")}>
              <select
                className="select"
                value={choice.initial_condition}
                onChange={(e) => set({ initial_condition: e.target.value })}
              >
                {INITIAL.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Machine" error={fieldError(err, "host_id")}>
              <select className="select" value={choice.host_id} onChange={(e) => set({ host_id: e.target.value })}>
                <option value="auto">Automatic</option>
                {(hosts.data ?? []).map((h) => (
                  <option key={h.id} value={h.id}>
                    {h.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Backend" error={fieldError(err, "backend")}>
              <select className="select" value={choice.backend} onChange={(e) => set({ backend: e.target.value })}>
                {BACKENDS.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <ErrorNote error={formError(err, SHOWN_FIELDS)} />
        </div>
        <div className="modal-foot">
          <span className="small muted spacer">
            {choice.candidates.length
              ? `${choice.candidates.length} scheme${choice.candidates.length === 1 ? "" : "s"} against ${choice.baseline}`
              : "Pick at least one scheme to test."}
          </span>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={!choice.candidates.length || action.busy}>
            {action.busy ? <Spinner /> : <Icon name="play" size={16} />}
            Propose experiment
          </button>
        </div>
      </form>
    </Modal>
  );
}
