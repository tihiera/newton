import { humanKey, plain } from "../text";

/** Every key of `details`, as agentd sent it (unknown kinds, and "All details"). */
export function GenericDetails({ details }: { details: Record<string, unknown> }) {
  const keys = Object.keys(details);
  if (!keys.length) return <div className="muted small">No details.</div>;
  return (
    <div className="ap-generic">
      {keys.map((k) => {
        const v = details[k];
        const text = plain(v);
        const block = typeof v === "object" && v !== null;
        return (
          <div className="ap-generic-row" key={k}>
            <span className="ap-generic-key">{humanKey(k)}</span>
            {block ? <pre className="ap-generic-pre mono">{text}</pre> : <span className="ap-generic-val">{text}</span>}
          </div>
        );
      })}
    </div>
  );
}
