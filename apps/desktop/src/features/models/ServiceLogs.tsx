// A model server's log, tailed from an offset: each read returns what was written
// since `offset` and the next offset to ask from.

import { useEffect, useRef, useState } from "react";
import type { LogChunk } from "../../api";
import { api } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { ErrorNote } from "../../components/ui";

const KEEP = 200_000; // characters kept on screen

export function ServiceLogs({ serviceId }: { serviceId: string }) {
  // What is on screen, where the next read starts, and the last chunk taken in. A chunk
  // is appended once, while rendering, and only if it starts where the text ends.
  const [log, setLog] = useState<{ offset: number; text: string; seen?: LogChunk }>({ offset: 0, text: "" });
  const box = useRef<HTMLPreElement>(null);
  const chunk = usePolling(() => api.services.logs(serviceId, log.offset), [serviceId], {
    interval: 2000,
    followEvents: false,
  });
  const c = chunk.data;
  if (c && c !== log.seen) {
    setLog(
      c.offset === log.offset
        ? { offset: c.next_offset, text: (log.text + c.data).slice(-KEEP), seen: c }
        : { ...log, seen: c },
    );
  }
  const text = log.text;

  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div className="stack" style={{ gap: 8 }}>
      {chunk.error ? <ErrorNote error={chunk.error} /> : null}
      <pre className="log" ref={box}>
        {text || (chunk.loading ? "Reading the log…" : "The log is empty.")}
      </pre>
    </div>
  );
}
