// A model server's log, tailed from an offset: each read returns what was written
// since `offset` and the next offset to ask from.

import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { usePolling } from "../../hooks/usePolling";
import { ErrorNote } from "../../components/ui";

const KEEP = 200_000; // characters kept on screen

export function ServiceLogs({ serviceId }: { serviceId: string }) {
  const offset = useRef(0);
  const [text, setText] = useState("");
  const box = useRef<HTMLPreElement>(null);
  const chunk = usePolling(() => api.services.logs(serviceId, offset.current), [serviceId], {
    interval: 2000,
    followEvents: false,
  });

  useEffect(() => {
    const c = chunk.data;
    if (!c || c.offset !== offset.current) return;
    offset.current = c.next_offset;
    if (c.data) setText((t) => (t + c.data).slice(-KEEP));
  }, [chunk.data]);

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
