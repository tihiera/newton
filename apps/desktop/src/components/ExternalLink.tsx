// A link to a web page outside Newton (arXiv, a DOI, a published report). A real
// <a href>, so hovering shows the address and a browser's middle click works; a plain
// click goes through the shell, which opens it in the user's browser (only the sites
// capabilities/default.json allows). Anything but http(s) is shown as text.

import { useState, type AnchorHTMLAttributes, type MouseEvent } from "react";
import { copyText, isWebUrl, openExternal } from "../app/platform";

type Props = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "target" | "onClick"> & { href: string };

/** Why a click didn't open the page (the shell's allowlist, capabilities/default.json). */
export const NOT_OPENED = "Newton only opens arXiv, DOI, GitHub and Notion links.";

export function ExternalLink({ href, children, rel, ...rest }: Props) {
  const [note, setNote] = useState<string | null>(null);
  if (!isWebUrl(href)) {
    return (
      <span className={rest.className} title={rest.title}>
        {children}
      </span>
    );
  }
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    // Never let the click navigate the app's own window to the page.
    e.preventDefault();
    setNote(null);
    // A link the shell won't open: say so, and hand the address over instead.
    openExternal(href).catch(async () => {
      const copied = await copyText(href);
      setNote(copied ? `${NOT_OPENED} The address was copied.` : `${NOT_OPENED} Address: ${href}`);
    });
  };
  return (
    <>
      <a {...rest} href={href} rel={rel ?? "noreferrer noopener"} title={note ?? rest.title} onClick={onClick}>
        {children}
      </a>
      {note ? (
        <span className="muted small" role="status" style={{ marginLeft: 6, userSelect: "text" }}>
          {note}
        </span>
      ) : null}
    </>
  );
}
