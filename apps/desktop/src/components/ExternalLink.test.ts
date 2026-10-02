import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const platform = vi.hoisted(() => ({
  openExternal: vi.fn<(url: string) => Promise<void>>(async () => undefined),
  copyText: vi.fn<(text: string) => Promise<boolean>>(async () => true),
}));
vi.mock("../app/platform", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../app/platform")>()),
  openExternal: platform.openExternal,
  copyText: platform.copyText,
}));

// The component is called as a plain function to reach its click handler (there is no
// DOM in these tests), so its one piece of state is a spy.
const setNote = vi.hoisted(() => vi.fn());
vi.mock("react", async (importOriginal) => {
  const react = await importOriginal<typeof import("react")>();
  return { ...react, useState: (init: unknown) => [init, setNote] };
});

const { ExternalLink, NOT_OPENED } = await import("./ExternalLink");

type AnchorProps = { href?: string; onClick?: (e: unknown) => void; title?: string };

/** The <a> the component renders for `href` (it sits in a fragment, before the note). */
function anchor(href: string): ReactElement<AnchorProps> {
  const out = ExternalLink({ href, children: "paper" }) as ReactElement<{ children: ReactElement[] }>;
  const a = [out.props.children].flat().find((c) => (c as ReactElement)?.type === "a");
  if (!a) throw new Error(`no <a> for ${href}`);
  return a as ReactElement<AnchorProps>;
}

function click(a: ReactElement<AnchorProps>) {
  const e = { preventDefault: vi.fn() };
  a.props.onClick?.(e);
  return e;
}

beforeEach(() => {
  platform.openExternal.mockReset().mockResolvedValue(undefined);
  platform.copyText.mockReset().mockResolvedValue(true);
  setNote.mockReset();
});

describe("ExternalLink", () => {
  it("opens a web link through the platform, never in the app's window", () => {
    const a = anchor("https://arxiv.org/abs/1101.4315");
    expect(a.props.href).toBe("https://arxiv.org/abs/1101.4315");
    const e = click(a);
    expect(e.preventDefault).toHaveBeenCalled();
    expect(platform.openExternal).toHaveBeenCalledWith("https://arxiv.org/abs/1101.4315");
  });

  it("says why a link the shell refused didn't open, and copies its address", async () => {
    platform.openExternal.mockRejectedValue(new Error("Not allowed to open url"));
    click(anchor("https://en.wikipedia.org/wiki/MUSCL_scheme"));
    await vi.waitFor(() => expect(setNote).toHaveBeenLastCalledWith(`${NOT_OPENED} The address was copied.`));
    expect(platform.copyText).toHaveBeenCalledWith("https://en.wikipedia.org/wiki/MUSCL_scheme");

    platform.copyText.mockResolvedValue(false);
    click(anchor("https://en.wikipedia.org/wiki/MUSCL_scheme"));
    await vi.waitFor(() =>
      expect(setNote).toHaveBeenLastCalledWith(`${NOT_OPENED} Address: https://en.wikipedia.org/wiki/MUSCL_scheme`),
    );
  });

  it("shows anything but http(s) as plain text", () => {
    for (const href of ["javascript:alert(1)", "/experiments/exp-1", "file:///etc/passwd", ""]) {
      const out = ExternalLink({ href, children: "paper" }) as ReactElement;
      expect(out.type).toBe("span");
      const html = renderToStaticMarkup(createElement(ExternalLink, { href, children: "paper" }));
      expect(html).toBe("<span>paper</span>");
    }
    expect(
      renderToStaticMarkup(createElement(ExternalLink, { href: "https://doi.org/10.1137/0721062", children: "doi" })),
    ).toBe('<a href="https://doi.org/10.1137/0721062" rel="noreferrer noopener">doi</a>');
  });
});
