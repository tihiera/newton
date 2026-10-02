// An approval's `details`, per kind, never hidden: the known kinds get a designed
// layout, and every kind keeps an "All details" list with everything agentd sent.

import type { Approval } from "../../api";
import { Icon } from "../../components/Icon";
import { ExperimentDetails } from "./details/ExperimentDetails";
import { GenericDetails } from "./details/GenericDetails";
import { PublishDetails } from "./details/PublishDetails";
import { ServiceDetails } from "./details/ServiceDetails";
import { shortCommit, str } from "./text";
import "./approvals.css";

export function ApprovalDetails({ approval }: { approval: Pick<Approval, "kind" | "details"> }) {
  const d = approval.details ?? {};
  let body;
  if (approval.kind === "execute_experiment") body = <ExperimentDetails details={d} />;
  else if (approval.kind === "start_service") body = <ServiceDetails details={d} />;
  else if (approval.kind === "publish_report") body = <PublishDetails details={d} />;
  else return <GenericDetails details={d} />;
  return (
    <div className="stack" style={{ gap: 10 }}>
      {body}
      <details className="ap-all">
        <summary>All details</summary>
        <GenericDetails details={d} />
      </details>
    </div>
  );
}

/** The line left of the actions: the repository commit for an experiment, the
 *  external-action warning for a publication. */
export function ApprovalFootnote({ approval }: { approval: Pick<Approval, "kind" | "details"> }) {
  if (approval.kind === "execute_experiment") {
    const commit = str(approval.details?.repository_commit);
    return (
      <span className="ap-foot-note" title={commit}>
        <Icon name="branch" size={17} />
        Repo commit {shortCommit(commit)}
      </span>
    );
  }
  if (approval.kind === "publish_report") {
    return (
      <span className="ap-foot-note">
        <Icon name="alert" size={18} />
        This action publishes externally.
      </span>
    );
  }
  return null;
}
