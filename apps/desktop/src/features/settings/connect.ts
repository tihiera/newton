// What Settings > Connections shows for GitHub and Notion, from agentd's GET /connectors
// and the sign-in flow's status. Only the choice of panel and labels: agentd runs the
// flows, keeps the tokens and writes every sentence shown.

import type { ConnectFlow, ConnectorAccount, Connectors, GithubDeviceStart } from "../../api";

export type Target = "github" | "notion";

/** loading: agentd not read yet; connected: the account and Disconnect; reauth: still
 *  connected, but agentd's renewal of the sign-in was rejected (connect again, or
 *  Disconnect); signing-in: a Connect flow is open; connect: the Connect button (a
 *  token stays a fallback); token: only the paste field (this build has no Connect
 *  for the target). */
export type Panel = "loading" | "connected" | "reauth" | "signing-in" | "connect" | "token";

/** Whether this build of agentd offers the one-click Connect for the target. */
export function canConnect(conns: Connectors | undefined, target: Target): boolean {
  return conns?.oauth?.[target] === true;
}

export function panelFor(conns: Connectors | undefined, target: Target, signingIn: boolean): Panel {
  if (!conns) return "loading";
  if (conns[target] && !needsReauth(conns, target)) return "connected";
  if (signingIn) return "signing-in";
  if (conns[target]) return "reauth";
  return canConnect(conns, target) ? "connect" : "token";
}

export function accountOf(conns: Connectors | undefined, target: Target): ConnectorAccount | null {
  return conns?.accounts?.[target] ?? null;
}

/** agentd tried to renew the Notion sign-in and Notion rejected it: the token it keeps
 *  no longer works until the user connects again. Only Notion's sign-in is renewed. */
export function needsReauth(conns: Connectors | undefined, target: Target): boolean {
  return target === "notion" && conns?.[target] === true && conns.accounts?.[target]?.needs_reauth === true;
}

export const REAUTH_NOTE = "Notion's sign-in expired: connect again";

/** Connected and working (undefined until agentd is read): what clears the last
 *  Connect attempt's notes when it turns true. A reconnect after an expired sign-in
 *  counts, although the row was "connected" all along. */
export function workingConnection(conns: Connectors | undefined, target: Target): boolean | undefined {
  if (!conns) return undefined;
  return conns[target] === true && !needsReauth(conns, target);
}

const METHODS: Record<ConnectorAccount["method"], string> = {
  oauth: "via Connect",
  token: "via token",
  gh: "via GitHub CLI",
};

export function methodLabel(method: string | null | undefined): string | null {
  return method && method in METHODS ? METHODS[method as ConnectorAccount["method"]] : null;
}

/** "octocat · via Connect"; just the method while agentd doesn't know the name yet. */
export function accountLine(account: ConnectorAccount | null): string | null {
  if (!account) return null;
  const parts = [account.name?.trim(), methodLabel(account.method)].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** A workspace icon Newton can draw itself (an emoji or a letter). Image links aren't
 *  loaded: the app's CSP allows no remote images, so those keep the brand tile. */
export function accountGlyph(icon: string | null | undefined): string | null {
  const text = icon?.trim();
  if (!text || /^[a-z][a-z0-9+.-]*:/i.test(text) || text.includes("/")) return null;
  return [...text].length <= 8 ? text : null;
}

/** Only an OAuth sign-in is an authorization the user can revoke on github.com. */
export function showsRevokeNote(target: Target, account: ConnectorAccount | null): boolean {
  return target === "github" && account?.method === "oauth";
}

export type FlowResult =
  | { kind: "waiting" }
  | { kind: "connected" }
  /** denied / expired / failed (agentd's sentence), or none (the flow is gone). */
  | { kind: "stopped"; message: string | null };

const NO_SENTENCE = "Sign-in didn't finish: start again.";

export function flowResult(flow: ConnectFlow | undefined): FlowResult {
  if (!flow || flow.state === "pending") return { kind: "waiting" };
  if (flow.state === "connected") return { kind: "connected" };
  if (flow.state === "none") return { kind: "stopped", message: null };
  return { kind: "stopped", message: flow.error?.trim() || NO_SENTENCE };
}

/** The note after Cancel, from the flow agentd answers with: none (cancelled) and
 *  connected (it finished meanwhile; the refreshed row shows it) need none. */
export function cancelNote(flow: ConnectFlow | undefined | null): string | null {
  if (!flow) return null;
  const result = flowResult(flow);
  return result.kind === "stopped" ? result.message : null;
}

/** An open Connect flow: GitHub's code to type, or Notion's authorize link. */
export interface FlowLinks {
  github?: GithubDeviceStart;
  url?: string;
}

/** A sign-in still pending in agentd (started before Settings opened): what it needs
 *  to show again. Without its code or link the row still offers Cancel. */
export function resumedFlow(target: Target, flow: ConnectFlow): FlowLinks {
  const uri = flow.verification_uri?.trim() || undefined;
  if (target === "notion") return { url: uri };
  const code = flow.user_code?.trim();
  return code && uri
    ? { github: { user_code: code, verification_uri: uri, expires_at: flow.expires_at ?? 0, interval: 5 } }
    : {};
}

/** What an open flow shows: GitHub's code (Open GitHub, Cancel), Notion's link (Open
 *  Notion, Cancel), or, knowing neither, only the wait and Cancel. */
export function flowView(target: Target, flow: FlowLinks): "code" | "link" | "wait" {
  if (target === "github" && flow.github) return "code";
  if (target === "notion" && flow.url) return "link";
  return "wait";
}

/** The row has just reached the connected panel (a flow, a pasted token, the GitHub CLI
 *  login, or another window): the last Connect attempt's notes are stale from then on. */
export function justConnected(was: boolean | undefined, now: boolean | undefined): boolean {
  return now === true && was !== true;
}

/** The notes under a row. A Connect attempt's notes (agentd's sentence when a flow
 *  stopped, a failed start or cancel) belong to the panels that offer signing in, never
 *  to the connected one; a failed Disconnect belongs to wherever the row is. */
export function rowNotes<E>(
  panel: Panel,
  notes: { stopped: string | null; start?: E; cancel?: E; disconnect?: E },
): { stopped: string | null; error: E | undefined } {
  const signingIn = panel === "connect" || panel === "signing-in" || panel === "token" || panel === "reauth";
  return {
    stopped: signingIn ? notes.stopped : null,
    error: (signingIn ? (notes.start ?? notes.cancel) : undefined) ?? notes.disconnect,
  };
}
