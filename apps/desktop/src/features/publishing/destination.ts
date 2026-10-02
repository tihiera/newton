// The publish request a destination choice maps to (docs/ui-handoff.md §5.6). Only
// the shape: agentd validates the repo and page id and explains what's wrong.

export type Choice = "gist" | "issue" | "notion";

export interface PublishRequest {
  target: "github" | "notion";
  destination: Record<string, unknown>;
}

export function publishRequest(choice: Choice, repo: string, parentPageId: string): PublishRequest {
  if (choice === "issue") return { target: "github", destination: { kind: "issue", repo: repo.trim() } };
  if (choice === "notion") return { target: "notion", destination: { parent_page_id: parentPageId.trim() } };
  return { target: "github", destination: {} };
}

/** What the choice still needs from the user before it can be sent. */
export function missingInput(choice: Choice, repo: string, parentPageId: string): string | null {
  if (choice === "issue" && !repo.trim()) return "repo";
  if (choice === "notion" && !parentPageId.trim()) return "parent_page_id";
  return null;
}

export function connectorOf(choice: Choice): "github" | "notion" {
  return choice === "notion" ? "notion" : "github";
}
