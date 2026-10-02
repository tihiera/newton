// Agentd's Markdown reports, rendered as React elements (never as HTML: nothing from
// a report can inject markup). Headings, paragraphs, lists, tables, rules, inline code,
// bold, italics, links, and images (loaded through an authenticated `loadImage`).

import { useEffect, useState, type ReactNode } from "react";

type ImageLoader = (src: string) => Promise<Blob>;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*|_[^_]+_|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const k = `${key}-${i++}`;
    if (tok.startsWith("`")) out.push(<code key={k}>{tok.slice(1, -1)}</code>);
    else if (tok.startsWith("**")) out.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("_")) out.push(<em key={k}>{tok.slice(1, -1)}</em>);
    else {
      const [, label, href] = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok) ?? [];
      const safe = /^https?:\/\//.test(href ?? "");
      out.push(
        safe ? (
          <a key={k} href={href} target="_blank" rel="noreferrer">
            {label}
          </a>
        ) : (
          <span key={k}>{label}</span>
        ),
      );
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function AuthedImage({ src, alt, load }: { src: string; alt: string; load?: ImageLoader }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    if (!load || /^https?:/.test(src)) return;
    let alive = true;
    let made: string | undefined;
    load(src)
      .then((b) => {
        made = URL.createObjectURL(b);
        if (alive) setUrl(made);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [src, load]);
  return url ? <img src={url} alt={alt} /> : <span className="muted small">[{alt}]</span>;
}

export function Markdown({ text, loadImage }: { text: string; loadImage?: ImageLoader }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let n = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${n++}`;
    if (!line.trim()) {
      i++;
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) {
      const Tag = (`h${h[1].length}` as "h1" | "h2" | "h3");
      blocks.push(<Tag key={key}>{inline(h[2], key)}</Tag>);
      i++;
      continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={key} />);
      i++;
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) rows.push(cells);
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <table key={key}>
          <thead>
            <tr>{head?.map((c, j) => <th key={j}>{inline(c, `${key}h${j}`)}</th>)}</tr>
          </thead>
          <tbody>
            {body.map((r, ri) => (
              <tr key={ri}>{r.map((c, j) => <td key={j}>{inline(c, `${key}${ri}-${j}`)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }
    if (/^\s*[-*] /.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*] /, ""));
        i++;
      }
      blocks.push(
        <ul key={key}>
          {items.map((it, j) => <li key={j}>{inline(it, `${key}-${j}`)}</li>)}
        </ul>,
      );
      continue;
    }
    const img = /^!\[([^\]]*)\]\(([^)]+)\)\s*$/.exec(line);
    if (img) {
      blocks.push(
        <p key={key}>
          <AuthedImage src={img[2]} alt={img[1]} load={loadImage} />
        </p>,
      );
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3}\s|\||\s*[-*] |!\[|-{3,})/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    blocks.push(<p key={key}>{inline(para.join(" "), key)}</p>);
  }
  return <div className="md">{blocks}</div>;
}
