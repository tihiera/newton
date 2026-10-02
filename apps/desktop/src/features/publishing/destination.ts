// The publish request a destination choice maps to (docs/ui-handoff.md §5.6). Only
// the shape: agentd validates the repo and page id and explains what's wrong.

import { AgentdError, type Connectors, type NotionPage } from "../../api";

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

/** From this many listed Notion pages on, the picker offers a search box (agentd
 *  returns at most 50, the most recently edited first). */
export const PAGE_SEARCH_FROM = 8;

export function needsPageSearch(listed: number, query: string): boolean {
  return query.trim() !== "" || listed >= PAGE_SEARCH_FROM;
}

/** One answer from GET /connectors/notion/pages, with the query it was asked for. */
export interface PageAnswer {
  query: string;
  pages: NotionPage[];
}

/** What the picker shows: the last answer for the current query (it stays while a
 *  new search loads) and how many pages the unfiltered list has. */
export interface PageList {
  shown?: NotionPage[];
  shownFor?: string;
  listed?: number;
}

/** Takes an answer only if it is for the current query: right after the query
 *  changes, the previous search's answer is still around and must not count as the
 *  unfiltered list. `listed` comes only from answers to the empty query. */
export function takePageAnswer(list: PageList, answer: PageAnswer, current: string): PageList {
  if (answer.query !== current) return list;
  return {
    shown: answer.pages,
    shownFor: answer.query,
    listed: answer.query === "" ? answer.pages.length : list.listed,
  };
}

/** "share": the integration sees no page at all; "no_match": a search found none. */
export function pageListHint(list: PageList): "share" | "no_match" | null {
  if (list.listed === 0) return "share";
  if (list.shown?.length === 0 && list.shownFor) return "no_match";
  return null;
}

/** Escape in the page search closes the list, not the dialog: the Modal listens for
 *  Escape on window, so the key must not travel on. */
export function closeListOnEscape(e: { key: string; stopPropagation: () => void }, close: () => void): void {
  if (e.key !== "Escape") return;
  e.stopPropagation();
  close();
}

const NAMES = { github: "GitHub", notion: "Notion" } as const;

/** agentd's sentence when the Notion sign-in can't be renewed any more (409
 *  notion_reauth from GET /connectors/notion/pages); null for any other answer. */
export function notionReauthMessage(error: unknown): string | null {
  return error instanceof AgentdError && error.status === 409 && error.code === "notion_reauth" ? error.message : null;
}

/** agentd's sentence for a rejected renewal, for when GET /connectors flagged it
 *  (needs_reauth) before the page list asked: e.g. while pasting a page id. */
export const NOTION_REAUTH = "Notion's sign-in expired: connect Notion again";

/** Why the choice can't be published yet, as the Open Settings note says it: not
 *  connected, or (Notion) its sign-in expired, in agentd's words. null while
 *  agentd's connectors aren't read yet, and once nothing stands in the way. */
export function settingsBlock(
  choice: Choice,
  conns: Connectors | undefined,
  notionReauth: string | null,
): string | null {
  if (!conns) return null;
  const target = connectorOf(choice);
  if (!conns[target]) return `Connect ${NAMES[target]} in Settings to publish here.`;
  if (target !== "notion") return null;
  if (notionReauth) return notionReauth;
  return conns.accounts?.notion?.needs_reauth === true ? NOTION_REAUTH : null;
}
