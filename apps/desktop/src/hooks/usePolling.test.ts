import { describe, expect, it } from "vitest";
import { MAX_BACKOFF, nextDelay } from "./usePolling";

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
