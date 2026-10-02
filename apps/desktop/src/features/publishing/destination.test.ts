import { describe, expect, it } from "vitest";
import type { NotionPage } from "../../api";
import {
  PAGE_SEARCH_FROM,
  closeListOnEscape,
  connectorOf,
  missingInput,
  needsPageSearch,
  pageListHint,
  publishRequest,
  takePageAnswer,
  type PageList,
} from "./destination";

describe("publishRequest", () => {
  it("maps each choice to agentd's shape", () => {
    expect(publishRequest("gist", "x", "y")).toEqual({ target: "github", destination: {} });
    expect(publishRequest("issue", " o/r ", "")).toEqual({ target: "github", destination: { kind: "issue", repo: "o/r" } });
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

const page = (n: number): NotionPage => ({ id: n.toString(16).padStart(32, "0"), title: `Page ${n}`, url: null, icon: null });
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
