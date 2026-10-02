import { describe, expect, it, vi } from "vitest";
import { createPlatform, FILE_NAME_HEADER, isWebUrl, type PlatformDeps } from "./platform";

function deps(tauri: boolean, over: Partial<PlatformDeps> = {}) {
  return {
    isTauri: () => tauri,
    openUrl: vi.fn<PlatformDeps["openUrl"]>(async () => undefined),
    invoke: vi.fn(async () => "/Users/me/Downloads/report.zip") as unknown as PlatformDeps["invoke"],
    windowOpen: vi.fn<PlatformDeps["windowOpen"]>(),
    download: vi.fn<PlatformDeps["download"]>(),
    writeText: vi.fn<PlatformDeps["writeText"]>(async () => undefined),
    ...over,
  };
}

describe("web urls", () => {
  it("accepts only http(s)", () => {
    expect(isWebUrl("https://arxiv.org/abs/2401.12345")).toBe(true);
    expect(isWebUrl("http://example.org")).toBe(true);
    expect(isWebUrl("javascript:alert(1)")).toBe(false);
    expect(isWebUrl("file:///etc/passwd")).toBe(false);
    expect(isWebUrl("/relative")).toBe(false);
    expect(isWebUrl(null)).toBe(false);
  });
});

describe("openExternal", () => {
  it("uses the opener plugin in the shell", async () => {
    const d = deps(true);
    await createPlatform(d).openExternal("https://doi.org/10.1016/j.jcp.2012.01.001");
    expect(d.openUrl).toHaveBeenCalledWith("https://doi.org/10.1016/j.jcp.2012.01.001");
    expect(d.windowOpen).not.toHaveBeenCalled();
  });

  it("opens a new tab in a plain browser", async () => {
    const d = deps(false);
    await createPlatform(d).openExternal("https://arxiv.org/abs/1101.4315");
    expect(d.windowOpen).toHaveBeenCalledWith("https://arxiv.org/abs/1101.4315");
    expect(d.openUrl).not.toHaveBeenCalled();
  });

  it("refuses non-web links and passes the shell's refusal on as an Error", async () => {
    const d = deps(true, {
      openUrl: vi.fn(async () => Promise.reject("Not allowed to open url https://evil.example")),
    });
    const p = createPlatform(d);
    await expect(p.openExternal("javascript:alert(1)")).rejects.toThrow(/only web links/);
    await expect(p.openExternal("https://evil.example")).rejects.toThrow(
      "Not allowed to open url https://evil.example",
    );
  });
});

describe("copyText", () => {
  it("says whether the clipboard took the text", async () => {
    const d = deps(true);
    expect(await createPlatform(d).copyText("https://en.wikipedia.org/wiki/MUSCL_scheme")).toBe(true);
    expect(d.writeText).toHaveBeenCalledWith("https://en.wikipedia.org/wiki/MUSCL_scheme");
    const refused = deps(true, { writeText: vi.fn(async () => Promise.reject(new Error("NotAllowedError"))) });
    expect(await createPlatform(refused).copyText("x")).toBe(false);
  });
});

describe("saveFile", () => {
  it("names the header as the shell reads it (files::NAME_HEADER)", () => {
    // src-tauri/src/files.rs pins the same literal in `name_header_matches_the_ui`.
    expect(FILE_NAME_HEADER).toBe("x-newton-file-name");
  });

  it("sends the bytes raw and the name in a header to save_file", async () => {
    const d = deps(true);
    const path = await createPlatform(d).saveFile("Schéma TVD-exp-1.zip", new Blob([new Uint8Array([1, 2, 3])]));
    expect(path).toBe("/Users/me/Downloads/report.zip");
    const [cmd, bytes, options] = (d.invoke as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(cmd).toBe("save_file");
    expect(Array.from(bytes as Uint8Array)).toEqual([1, 2, 3]);
    expect(options).toEqual({ headers: { [FILE_NAME_HEADER]: "Sch%C3%A9ma%20TVD-exp-1.zip" } });
  });

  it("returns null when the user cancels, and the shell's sentence on failure", async () => {
    const cancelled = deps(true, { invoke: (async () => null) as PlatformDeps["invoke"] });
    expect(await createPlatform(cancelled).saveFile("a.zip", new Blob([]))).toBeNull();
    const failing = deps(true, {
      invoke: (async () => Promise.reject("couldn't write /x/a.zip: permission denied")) as PlatformDeps["invoke"],
    });
    await expect(createPlatform(failing).saveFile("a.zip", new Blob([]))).rejects.toThrow(
      "couldn't write /x/a.zip: permission denied",
    );
  });

  it("downloads through the browser outside the shell", async () => {
    const d = deps(false);
    const blob = new Blob(["zip"]);
    expect(await createPlatform(d).saveFile("a.zip", blob)).toBe("a.zip");
    expect(d.download).toHaveBeenCalledWith("a.zip", blob);
    expect(d.invoke).not.toHaveBeenCalled();
  });
});
