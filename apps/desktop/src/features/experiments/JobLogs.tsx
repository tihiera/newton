// A job's stdout or stderr, tailed by offset while shown (useJobLogs).

import { useEffect, useRef, useState } from "react";
import { useJobLogs } from "../../hooks/useJobLogs";

export function JobLogs({ jobId, finished }: { jobId: string; finished: boolean }) {
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const text = useJobLogs(jobId, stream, true, finished);
  const box = useRef<HTMLPreElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div className="ex-logs">
      <div className="row ex-log-tabs" role="tablist">
        {(["stdout", "stderr"] as const).map((s) => (
          <button
            key={s}
            role="tab"
            aria-selected={stream === s}
            className={`btn ${stream === s ? "mesh-tab-active" : "ghost"}`}
            onClick={() => setStream(s)}
          >
            {s}
          </button>
        ))}
      </div>
      <pre
        className="log"
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
      >
        {text || (finished ? "(empty)" : "Waiting for output…")}
      </pre>
    </div>
  );
}
