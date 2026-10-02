import { describe, expect, it } from "vitest";
import { AgentdError, type Connectors, type NotionPage } from "../../api";
import {
  NOTION_REAUTH,
  PAGE_SEARCH_FROM,
  closeListOnEscape,
  connectorOf,
  missingInput,
  needsPageSearch,
  notionReauthMessage,
  pageListHint,
  publishRequest,
  settingsBlock,
  takePageAnswer,
  type PageList,
} from "./destination";

describe("publishRequest", () => {
  it("maps each choice to agentd's shape", () => {
    expect(publishRequest("gist", "x", "y")).toEqual({ target: "github", destination: {} });
    expect(publishRequest("issue", " o/r ", "")).toEqual({
      target: "github",
      destination: { kind: "issue", repo: "o/r" },
    });
    expect(publishRequest("notion", "", " abc ")).toEqual({ target: "notion", destination: { parent_page_id: "abc" } });
  });
  it("knows what is missing", () => {
    expect(missingInput("gist", "", "")).toBeNull();
    expect(missingInput("issue", "", "")).toBe("repo");
    expect(missingInput("notion", "", "")).toBe("parent_page_id");
    expect(connectorOf("issue")).toBe("github");
  });
});

describe("needsPageSearch", () => {
  it("offers search for long lists and keeps it while a query is typed", () => {
    expect(needsPageSearch(3, "")).toBe(false);
    expect(needsPageSearch(PAGE_SEARCH_FROM, "")).toBe(true);
    expect(needsPageSearch(0, "journal")).toBe(true);
    expect(needsPageSearch(2, "  ")).toBe(false);
  });
});

const page = (n: number): NotionPage => ({
  id: n.toString(16).padStart(32, "0"),
  title: `Page ${n}`,
  url: null,
  icon: null,
});
const pages = (count: number) => Array.from({ length: count }, (_, i) => page(i + 1));

describe("takePageAnswer", () => {
  it("counts the unfiltered list and keeps that count while searching", () => {
    let list = takePageAnswer({}, { query: "", pages: pages(20) }, "");
    expect(list.listed).toBe(20);
    list = takePageAnswer(list, { query: "zzz", pages: [] }, "zzz");
    expect(list).toEqual({ shown: [], shownFor: "zzz", listed: 20 });
  });
  it("ignores a search's answer once the query has been cleared", () => {
    const searched: PageList = { shown: [], shownFor: "zzz", listed: 20 };
    // The render where the query becomes "" still holds the "zzz" answer.
    const list = takePageAnswer(searched, { query: "zzz", pages: [] }, "");
    expect(list).toBe(searched);
    expect(list.listed).toBe(20);
    expect(needsPageSearch(list.listed ?? 0, "")).toBe(true);
    expect(pageListHint(list)).toBe("no_match");
    const short = takePageAnswer(searched, { query: "jour", pages: pages(3) }, "");
    expect(short.listed).toBe(20);
  });
  it("takes the unfiltered answer when it arrives", () => {
    const list = takePageAnswer({ shown: [], shownFor: "zzz", listed: 20 }, { query: "", pages: pages(20) }, "");
    expect(list.shown).toHaveLength(20);
    expect(pageListHint(list)).toBeNull();
  });
});

describe("pageListHint", () => {
  it("asks to share a page only when the unfiltered list is empty", () => {
    expect(pageListHint({})).toBeNull();
    expect(pageListHint(takePageAnswer({}, { query: "", pages: [] }, ""))).toBe("share");
    expect(pageListHint({ shown: pages(2), shownFor: "", listed: 2 })).toBeNull();
  });
  it("says no page matches only for a search that found none", () => {
    expect(pageListHint({ shown: [], shownFor: "zzz", listed: 20 })).toBe("no_match");
    expect(pageListHint({ shown: [], shownFor: "zzz" })).toBe("no_match");
    expect(pageListHint({ shown: pages(1), shownFor: "zzz", listed: 20 })).toBeNull();
  });
});

describe("closeListOnEscape", () => {
  const key = (k: string) => {
    const e = { key: k, stopped: false, stopPropagation: () => void (e.stopped = true) };
    return e;
  };
  it("closes the list and keeps Escape from reaching the dialog", () => {
    let closed = 0;
    const e = key("Escape");
    closeListOnEscape(e, () => closed++);
    expect(closed).toBe(1);
    expect(e.stopped).toBe(true);
  });
  it("leaves other keys alone", () => {
    let closed = 0;
    const e = key("a");
    closeListOnEscape(e, () => closed++);
    expect(closed).toBe(0);
    expect(e.stopped).toBe(false);
  });
});

const SENTENCE = "Notion's sign-in expired: connect Notion again";
const reauth409 = () =>
  new AgentdError("http", SENTENCE, { status: 409, code: "notion_reauth", path: "/connectors/notion/pages" });

describe("notionReauthMessage", () => {
  it("takes agentd's sentence from a 409 notion_reauth only", () => {
    expect(notionReauthMessage(reauth409())).toBe(SENTENCE);
    expect(notionReauthMessage(new AgentdError("http", "busy", { status: 409, code: "other" }))).toBeNull();
    expect(
      notionReauthMessage(
        new AgentdError("http", "Notion couldn't be reached to renew the sign-in: try again in a moment", {
          status: 502,
        }),
      ),
    ).toBeNull();
    expect(notionReauthMessage(new Error("boom"))).toBeNull();
    expect(notionReauthMessage(undefined)).toBeNull();
  });
});

describe("settingsBlock", () => {
  const conns = (over: Partial<Connectors> = {}): Connectors => ({ github: true, notion: true, ...over });
  it("waits for agentd's connectors", () => {
    expect(settingsBlock("notion", undefined, SENTENCE)).toBeNull();
  });
  it("sends a missing connection to Settings", () => {
    expect(settingsBlock("issue", conns({ github: false }), null)).toBe("Connect GitHub in Settings to publish here.");
    expect(settingsBlock("notion", conns({ notion: false }), SENTENCE)).toBe(
      "Connect Notion in Settings to publish here.",
    );
  });
  it("sends an expired Notion sign-in to Settings in agentd's words", () => {
    expect(settingsBlock("notion", conns(), notionReauthMessage(reauth409()))).toBe(SENTENCE);
    expect(settingsBlock("gist", conns(), SENTENCE)).toBeNull();
    expect(settingsBlock("notion", conns(), null)).toBeNull();
  });
  it("reads agentd's flag before the page list has asked", () => {
    const flagged = conns({
      accounts: {
        github: null,
        notion: { name: "Lab", icon: null, method: "oauth", connected_at: 1, needs_reauth: true },
      },
    });
    expect(settingsBlock("notion", flagged, null)).toBe(NOTION_REAUTH);
    expect(NOTION_REAUTH).toBe(SENTENCE);
    expect(settingsBlock("issue", flagged, null)).toBeNull();
  });
});
