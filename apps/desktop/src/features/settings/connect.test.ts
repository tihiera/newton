import { describe, expect, it } from "vitest";
import type { ConnectorAccount, Connectors } from "../../api";
import {
  accountGlyph,
  accountLine,
  accountOf,
  canConnect,
  cancelNote,
  flowResult,
  flowView,
  justConnected,
  methodLabel,
  needsReauth,
  panelFor,
  REAUTH_NOTE,
  resumedFlow,
  rowNotes,
  showsRevokeNote,
  workingConnection,
} from "./connect";

const account = (over: Partial<ConnectorAccount> = {}): ConnectorAccount => ({
  name: "octocat",
  icon: null,
  method: "oauth",
  connected_at: 1_700_000_000,
  ...over,
});

describe("panelFor", () => {
  it("waits for agentd", () => {
    expect(panelFor(undefined, "github", false)).toBe("loading");
    expect(panelFor(undefined, "notion", true)).toBe("loading");
  });
  it("shows the account once connected, even while a flow is open", () => {
    const conns: Connectors = { github: true, notion: false, oauth: { github: true, notion: true } };
    expect(panelFor(conns, "github", false)).toBe("connected");
    expect(panelFor(conns, "github", true)).toBe("connected");
  });
  it("offers Connect only when the build has it", () => {
    const conns: Connectors = { github: false, notion: false, oauth: { github: true, notion: false } };
    expect(panelFor(conns, "github", false)).toBe("connect");
    expect(panelFor(conns, "notion", false)).toBe("token");
    expect(panelFor(conns, "github", true)).toBe("signing-in");
  });
  it("falls back to the paste field with an older agentd", () => {
    const conns: Connectors = { github: false, notion: false };
    expect(canConnect(conns, "github")).toBe(false);
    expect(panelFor(conns, "github", false)).toBe("token");
    expect(panelFor(conns, "notion", false)).toBe("token");
  });
});

describe("accounts", () => {
  it("reads the account when agentd knows it", () => {
    const a = account();
    expect(accountOf({ github: true, notion: false, accounts: { github: a, notion: null } }, "github")).toBe(a);
    expect(accountOf({ github: true, notion: false, accounts: { github: a, notion: null } }, "notion")).toBeNull();
    expect(accountOf({ github: true, notion: false }, "github")).toBeNull();
    expect(accountOf(undefined, "github")).toBeNull();
  });
  it("labels each method", () => {
    expect(methodLabel("oauth")).toBe("via Connect");
    expect(methodLabel("token")).toBe("via token");
    expect(methodLabel("gh")).toBe("via GitHub CLI");
    expect(methodLabel("telepathy")).toBeNull();
    expect(methodLabel(undefined)).toBeNull();
  });
  it("joins the name and the method", () => {
    expect(accountLine(account())).toBe("octocat · via Connect");
    expect(accountLine(account({ name: "", method: "token" }))).toBe("via token");
    expect(accountLine(account({ name: "Lab", method: "gh" }))).toBe("Lab · via GitHub CLI");
    expect(accountLine(null)).toBeNull();
  });
  it("draws emoji icons and leaves image links alone", () => {
    expect(accountGlyph("🧪")).toBe("🧪");
    expect(accountGlyph("👩‍🔬")).toBe("👩‍🔬");
    expect(accountGlyph("L")).toBe("L");
    expect(accountGlyph("https://avatars.githubusercontent.com/u/1?v=4")).toBeNull();
    expect(accountGlyph("data:image/png;base64,AAAA")).toBeNull();
    expect(accountGlyph("//cdn.example/x.png")).toBeNull();
    expect(accountGlyph("a very long workspace label")).toBeNull();
    expect(accountGlyph("  ")).toBeNull();
    expect(accountGlyph(null)).toBeNull();
  });
  it("notes revoking on github.com only for a GitHub sign-in", () => {
    expect(showsRevokeNote("github", account())).toBe(true);
    expect(showsRevokeNote("github", account({ method: "token" }))).toBe(false);
    expect(showsRevokeNote("github", account({ method: "gh" }))).toBe(false);
    expect(showsRevokeNote("github", null)).toBe(false);
    expect(showsRevokeNote("notion", account())).toBe(false);
  });
});

describe("flowResult", () => {
  it("keeps waiting while pending", () => {
    expect(flowResult(undefined)).toEqual({ kind: "waiting" });
    expect(flowResult({ state: "pending", user_code: "ABCD-1234" })).toEqual({ kind: "waiting" });
  });
  it("finishes on connected", () => {
    expect(flowResult({ state: "connected", account: account() })).toEqual({ kind: "connected" });
  });
  it("shows agentd's sentence verbatim when it stops", () => {
    expect(flowResult({ state: "denied", error: "you declined on GitHub" })).toEqual({
      kind: "stopped",
      message: "you declined on GitHub",
    });
    expect(flowResult({ state: "expired", error: "the code expired: start again" })).toEqual({
      kind: "stopped",
      message: "the code expired: start again",
    });
    expect(flowResult({ state: "failed", error: null })).toEqual({
      kind: "stopped",
      message: "Sign-in didn't finish: start again.",
    });
  });
  it("stops quietly when the flow is gone", () => {
    expect(flowResult({ state: "none" })).toEqual({ kind: "stopped", message: null });
  });
});

describe("rowNotes", () => {
  const failed = new Error("GitHub answered 503: unavailable");
  const cancelFailed = new Error("agentd is not running");
  const disconnectFailed = new Error("couldn't reach the Keychain");

  it("shows a failed start, a failed cancel and a stopped flow while signing in is offered", () => {
    for (const panel of ["connect", "signing-in", "token"] as const) {
      expect(rowNotes(panel, { stopped: "you declined on GitHub", start: failed })).toEqual({
        stopped: "you declined on GitHub",
        error: failed,
      });
      expect(rowNotes(panel, { stopped: null, cancel: cancelFailed })).toEqual({ stopped: null, error: cancelFailed });
    }
  });
  it("drops a failed Connect once a pasted token connected the row", () => {
    const notes = { stopped: "the code expired: start again", start: failed, cancel: cancelFailed };
    expect(rowNotes("connected", notes)).toEqual({ stopped: null, error: undefined });
  });
  it("keeps a failed Disconnect on the connected panel", () => {
    expect(rowNotes("connected", { stopped: null, start: failed, disconnect: disconnectFailed })).toEqual({
      stopped: null,
      error: disconnectFailed,
    });
  });
});

describe("justConnected", () => {
  it("fires once when the row reaches connected", () => {
    expect(justConnected(false, true)).toBe(true);
    expect(justConnected(undefined, true)).toBe(true);
    expect(justConnected(true, true)).toBe(false);
    expect(justConnected(true, false)).toBe(false);
    expect(justConnected(false, false)).toBe(false);
    expect(justConnected(undefined, false)).toBe(false);
  });
  it("leaves no declined sentence for after a later Disconnect", () => {
    // Decline on GitHub, connect with a token, then disconnect: the row's notes as the
    // component keeps them, step by step.
    let stopped: string | null = "you declined on GitHub";
    let was: boolean | undefined = false;
    for (const now of [false, true, true, false]) {
      if (justConnected(was, now)) stopped = null;
      was = now;
    }
    expect(rowNotes("connect", { stopped })).toEqual({ stopped: null, error: undefined });
  });
});

describe("needsReauth", () => {
  const expired = (over: Partial<Connectors> = {}): Connectors => ({
    github: true,
    notion: true,
    oauth: { github: true, notion: true },
    accounts: { github: account({ needs_reauth: true }), notion: account({ name: "Lab", needs_reauth: true }) },
    ...over,
  });
  it("flags Notion only, and only while connected", () => {
    expect(needsReauth(expired(), "notion")).toBe(true);
    expect(needsReauth(expired(), "github")).toBe(false);
    expect(needsReauth(expired({ notion: false }), "notion")).toBe(false);
    expect(needsReauth({ github: true, notion: true }, "notion")).toBe(false);
    expect(needsReauth(undefined, "notion")).toBe(false);
  });
  it("offers Connect again (and Disconnect) instead of the connected panel", () => {
    expect(panelFor(expired(), "notion", false)).toBe("reauth");
    expect(panelFor(expired(), "notion", true)).toBe("signing-in");
    expect(panelFor(expired(), "github", false)).toBe("connected");
    expect(REAUTH_NOTE).toBe("Notion's sign-in expired: connect again");
  });
  it("keeps a failed Connect visible on the reauth panel", () => {
    const failed = new Error("Notion answered 503");
    expect(rowNotes("reauth", { stopped: "you declined", start: failed })).toEqual({
      stopped: "you declined",
      error: failed,
    });
  });
  it("counts a reconnect after an expired sign-in as just connected", () => {
    const was = workingConnection(expired(), "notion");
    const now = workingConnection(
      expired({ accounts: { github: null, notion: account({ name: "Lab", needs_reauth: false }) } }),
      "notion",
    );
    expect(was).toBe(false);
    expect(now).toBe(true);
    expect(justConnected(was, now)).toBe(true);
    expect(workingConnection(undefined, "notion")).toBeUndefined();
    expect(workingConnection({ github: false, notion: false }, "github")).toBe(false);
  });
});

describe("cancelNote", () => {
  it("is quiet when the flow is gone or finished meanwhile", () => {
    expect(cancelNote({ state: "none" })).toBeNull();
    expect(cancelNote({ state: "connected", account: account() })).toBeNull();
    expect(cancelNote(undefined)).toBeNull();
    expect(cancelNote(null)).toBeNull();
  });
  it("keeps agentd's sentence when the flow had already stopped", () => {
    expect(cancelNote({ state: "denied", error: "you declined on GitHub" })).toBe("you declined on GitHub");
  });
});

describe("resumedFlow", () => {
  it("reopens Notion's authorize link from verification_uri", () => {
    const url = "https://api.notion.com/v1/oauth/authorize?client_id=x&state=y";
    const links = resumedFlow("notion", { state: "pending", verification_uri: url, expires_at: 9 });
    expect(links).toEqual({ url });
    expect(flowView("notion", links)).toBe("link");
  });
  it("still offers Cancel without the link", () => {
    for (const verification_uri of [undefined, null, "  "]) {
      const links = resumedFlow("notion", { state: "pending", verification_uri });
      expect(links).toEqual({ url: undefined });
      expect(flowView("notion", links)).toBe("wait");
    }
  });
  it("shows GitHub's code again when agentd has it", () => {
    const links = resumedFlow("github", {
      state: "pending",
      user_code: "ABCD-1234",
      verification_uri: "https://github.com/login/device",
      expires_at: 99,
    });
    expect(links.github).toEqual({
      user_code: "ABCD-1234",
      verification_uri: "https://github.com/login/device",
      expires_at: 99,
      interval: 5,
    });
    expect(flowView("github", links)).toBe("code");
    const bare = resumedFlow("github", { state: "pending", verification_uri: "https://github.com/login/device" });
    expect(bare).toEqual({});
    expect(flowView("github", bare)).toBe("wait");
  });
  it("matches a flow's links to its target", () => {
    expect(flowView("github", { url: "https://notion.so" })).toBe("wait");
  });
});
