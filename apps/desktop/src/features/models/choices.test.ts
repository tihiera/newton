import { describe, expect, it } from "vitest";
import { memoryFor, serviceName } from "./choices";

describe("memoryFor", () => {
  it("keeps the model's hint up to 8k, then adds the attention cache", () => {
    expect(memoryFor(12, 8192, 1)).toBe(12); // qwen3:14b at the default
    expect(memoryFor(12, 4096, 1)).toBe(12);
    expect(memoryFor(12, 32768, 1)).toBe(17); // +5 GB for 24k more tokens
    expect(memoryFor(12, 32768, 2)).toBe(23); // two requests: twice the cache
    expect(memoryFor(2, 32768, 1)).toBe(5); // a small model: at least 4 GB per 32k
  });
});

describe("serviceName", () => {
  it("makes a service name from a model", () => {
    expect(serviceName("qwen3:14b")).toBe("qwen3-14b");
  });
});
