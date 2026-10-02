// Outline icons (1.6px strokes, 24px grid): one consistent set, drawn inline.

const PATHS: Record<string, string> = {
  paper: "M7 3h7l5 5v13H7z M14 3v5h5 M10 12h6 M10 16h6",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  plus: "M12 5v14 M5 12h14",
  search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14z M20 20l-4-4",
  chevronLeft: "M15 6l-6 6 6 6",
  chevronRight: "M9 6l6 6-6 6",
  chevronDown: "M6 9l6 6 6-6",
  chevronUp: "M6 15l6-6 6 6",
  clock: "M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z M12 7v5l3 2",
  flask: "M9 3h6 M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3 M7.5 15h9",
  check: "M5 12.5l4.5 4.5L19 7",
  x: "M6 6l12 12 M18 6L6 18",
  share: "M12 15V4 M8 8l4-4 4 4 M5 12v7h14v-7",
  settings:
    "M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  chip: "M7 7h10v10H7z M10 10h4v4h-4z M9 3v4 M15 3v4 M9 17v4 M15 17v4 M3 9h4 M3 15h4 M17 9h4 M17 15h4",
  laptop: "M5 6h14v9H5z M3 18h18",
  server: "M4 5h16v6H4z M4 13h16v6H4z M7 8h.01 M7 16h.01",
  bars: "M6 20V11 M12 20V5 M18 20v-6",
  wave: "M3 15c3 0 3-8 6-8s3 10 6 10 3-6 6-6",
  cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9z M4 7.5l8 4.5 8-4.5 M12 12v9",
  graph:
    "M6 18a2 2 0 1 1 0-4 2 2 0 0 1 0 4z M18 18a2 2 0 1 1 0-4 2 2 0 0 1 0 4z M12 8a2 2 0 1 1 0-4 2 2 0 0 1 0 4z M11 8l-4 6 M13 8l4 6",
  target: "M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8z M12 11.5v1",
  play: "M8 5l11 7-11 7z",
  alert: "M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z M12 8v5 M12 16h.01",
  info: "M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z M12 11v5 M12 8h.01",
  branch: "M6 3v12 M18 9a3 3 0 1 1 0-6 3 3 0 0 1 0 6z M6 21a3 3 0 1 1 0-6 3 3 0 0 1 0 6z M18 9a9 9 0 0 1-9 9",
  more: "M5 12h.01 M12 12h.01 M19 12h.01",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7 M20 4v7h-7",
  trash: "M4 7h16 M10 11v6 M14 11v6 M6 7l1 13h10l1-13 M9 7V4h6v3",
  key: "M15 7a4 4 0 1 1-3.5 6L5 19.5V21H3v-3l6.5-6.5A4 4 0 0 1 15 7z M16 8h.01",
  copy: "M9 9h11v11H9z M5 15H4V4h11v1",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  list: "M8 6h12 M8 12h12 M8 18h12 M4 6h.01 M4 12h.01 M4 18h.01",
  database:
    "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  pause: "M8 5v14 M16 5v14",
  archive: "M3 5h18v4H3z M5 9v10h14V9 M10 13h4",
  github:
    "M9 19c-4.5 1.5-4.5-2.5-6-3 M15 22v-3.9a3.4 3.4 0 0 0-.9-2.6c3.1-.4 6.4-1.5 6.4-6.8A5.3 5.3 0 0 0 19 4.8 4.9 4.9 0 0 0 18.9 1S17.7.6 15 2.5a13.4 13.4 0 0 0-7 0C5.3.6 4.1 1 4.1 1A4.9 4.9 0 0 0 4 4.8a5.3 5.3 0 0 0-1.5 3.8c0 5.3 3.3 6.4 6.4 6.8A3.4 3.4 0 0 0 8 18v4",
  notion: "M5 4h11l3 3v13H5z M9 8v8 M9 8l6 8 M15 8v8",
  sparkle:
    "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z M19 16l.7 1.8 1.8.7-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7z",
  logs: "M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5",
  stop: "M7 7h10v10H7z",
  model: "M12 3l8 4.5v9L12 21l-8-4.5v-9z M12 12l8-4.5 M12 12v9 M12 12L4 7.5",
};

export function Icon({
  name,
  size = 20,
  className,
  title,
}: {
  name: keyof typeof PATHS | string;
  size?: number;
  className?: string;
  title?: string;
}) {
  const d = PATHS[name] ?? PATHS.info;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
    >
      {title ? <title>{title}</title> : null}
      {d.split(" M").map((seg, i) => (
        <path key={i} d={i === 0 ? seg : `M${seg}`} />
      ))}
    </svg>
  );
}
