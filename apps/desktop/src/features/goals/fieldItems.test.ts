import { describe, expect, it } from "vitest";
import { listErrors, listKeys, splitList } from "./fieldItems";

const PATTERN = "String should match pattern '^[\\w .+-]+$'";

describe("splitList", () => {
  it("trims and drops empty entries", () => {
    expect(splitList(" a, b,, c ,")).toEqual(["a", "b", "c"]);
    expect(splitList("")).toEqual([]);
  });
});

describe("listErrors", () => {
  it("quotes the offending item as the dialog split the input", () => {
    const items = splitList("flux limiter, (evil)");
    const fields = { keywords: PATTERN, "keywords.1": PATTERN };
    expect(listErrors(fields, "keywords", items)).toEqual([`“(evil)”: ${PATTERN}`]);
  });

  it("names every offending item, in input order", () => {
    const fields = { keywords: "bad b", "keywords.3": "bad d", "keywords.1": "bad b" };
    expect(listErrors(fields, "keywords", ["a", "b", "c", "d"])).toEqual(["“b”: bad b", "“d”: bad d"]);
  });

  it("keeps the list's own message when no item carries it", () => {
    expect(listErrors({ keywords: "List should have at most 20 items" }, "keywords", ["a"])).toEqual([
      "List should have at most 20 items",
    ]);
  });

  it("shows the message unquoted for an item it can't find", () => {
    expect(listErrors({ keywords: "bad", "keywords.4": "bad" }, "keywords", ["a"])).toEqual(["bad"]);
  });

  it("ignores other lists and fields", () => {
    const fields = { title: "required", categories: "bad", "categories.0": "bad", "keywords_extra.0": "x" };
    expect(listErrors(fields, "keywords", ["a"])).toEqual([]);
    expect(listErrors(fields, "categories", ["math.XX"])).toEqual(["“math.XX”: bad"]);
  });
});

describe("listKeys", () => {
  it("lists the name and its dotted item paths", () => {
    const fields = { title: "t", keywords: "k", "keywords.0": "k", "keywords.2": "k", "categories.1": "c" };
    expect(listKeys(fields, "keywords")).toEqual(["keywords", "keywords.0", "keywords.2"]);
    expect(listKeys({}, "categories")).toEqual(["categories"]);
  });
});
