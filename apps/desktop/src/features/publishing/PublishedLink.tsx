// Where a published report lives: "Open on GitHub" / "Open in Notion", next to the
// experiment's Publish button and in the approval once it was approved.

import { api, type Publication } from "../../api";
import { ExternalLink } from "../../components/ExternalLink";
import { Icon } from "../../components/Icon";
import { Note, Spinner } from "../../components/ui";
import { usePolling } from "../../hooks/usePolling";

const OPEN: Record<Publication["target"], string> = { github: "Open on GitHub", notion: "Open in Notion" };

/** The experiment's published reports, newest first, one button each. */
export function PublishedLinks({ experimentId }: { experimentId: string }) {
  const pubs = usePolling(() => api.publishing.list(experimentId), [experimentId], { interval: 8000 });
  const published = (pubs.data ?? []).filter((p) => p.state === "published" && p.url);
  return (
    <>
      {published.map((p) => (
        <ExternalLink key={p.id} className="btn" href={p.url ?? ""}>
          <Icon name={p.target === "github" ? "github" : "notion"} size={17} />
          {OPEN[p.target]}
        </ExternalLink>
      ))}
    </>
  );
}

/** In an approved publication's review: sending, the link, or why it failed. */
export function PublicationOutcome({ experimentId, publicationId }: { experimentId: string; publicationId: string }) {
  const pubs = usePolling(() => api.publishing.list(experimentId), [experimentId], { interval: 2000 });
  const pub = pubs.data?.find((p) => p.id === publicationId);
  if (!pub) return null;
  if (pub.state === "published" && pub.url) {
    return (
      <div className="pub-outcome">
        <Icon name="check" size={18} />
        <span>Published.</span>
        <ExternalLink className="btn primary" href={pub.url}>
          <Icon name={pub.target === "github" ? "github" : "notion"} size={17} />
          {OPEN[pub.target]}
        </ExternalLink>
      </div>
    );
  }
  if (pub.state === "failed") {
    return (
      <Note tone="error" icon="alert">
        {pub.error ?? "Publishing failed."}
      </Note>
    );
  }
  if (pub.state === "approved" || pub.state === "publishing") {
    return (
      <div className="pub-outcome muted">
        <Spinner /> Publishing…
      </div>
    );
  }
  return null;
}
