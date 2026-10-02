// The experiment's Markdown report as agentd wrote it, with its images loaded
// through the authenticated client.

import { useCallback } from "react";
import { api } from "../../api";
import { Markdown } from "../../components/Markdown";
import { ErrorNote, Modal, Spinner } from "../../components/ui";
import { usePolling } from "../../hooks/usePolling";

export function ReportModal({
  experimentId,
  title,
  onClose,
}: {
  experimentId: string;
  title: string;
  onClose: () => void;
}) {
  const report = usePolling(() => api.experiments.report(experimentId), [experimentId], {
    interval: 0,
    followEvents: false,
  });
  const loadImage = useCallback((src: string) => api.experiments.reportFile(experimentId, src), [experimentId]);
  return (
    <Modal onClose={onClose} wide label={`Report: ${title}`}>
      <div className="modal-body">
        {report.data !== undefined ? (
          <Markdown text={report.data} loadImage={loadImage} />
        ) : report.error ? (
          <ErrorNote error={report.error} />
        ) : (
          <div className="row muted">
            <Spinner /> Loading the report…
          </div>
        )}
      </div>
    </Modal>
  );
}
