import { describe, expect, it } from "vitest";
import { MAX_BACKOFF, nextDelay, sameDeps } from "./usePolling";

describe("polling backoff", () => {
  it("polls at the interval while healthy, then backs off up to 30 s", () => {
    expect(nextDelay(4000, 0)).toBe(4000);
    expect(nextDelay(4000, 1)).toBe(4000);
    expect(nextDelay(4000, 2)).toBe(8000);
    expect(nextDelay(4000, 3)).toBe(16000);
    expect(nextDelay(4000, 10)).toBe(MAX_BACKOFF);
    expect(nextDelay(500, 2)).toBe(4000); // never faster than 2 s while failing
  });
});

describe("sameDeps", () => {
  it("is the same subject only for the same values in order", () => {
    expect(sameDeps(["a", 1], ["a", 1])).toBe(true);
    expect(sameDeps(["a", 1], ["a", 2])).toBe(false);
    expect(sameDeps(["a"], ["a", undefined])).toBe(false);
    expect(sameDeps([NaN, null], [NaN, null])).toBe(true);
    expect(sameDeps([{}], [{}])).toBe(false); // objects by identity
  });
});
