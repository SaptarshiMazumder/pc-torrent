import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { retryJobChunk } from "../../services/api";
import { getRetriedJobIds, markJobRetried } from "../../utils/retriedJobsCache";
import { clearTerminalDetailFromCache } from "../../utils/terminalDetailCache";

function rangeLabel(task) {
  const start = task.remaining_frame_start ?? task.frame_start;
  const end = task.remaining_frame_end ?? task.frame_end;
  if (start == null || end == null) return "—";
  return start === end ? `${start}` : `${start}-${end}`;
}

function FailedChunkCell({ task, backendUrl, groupId, onRetried, onRefresh, onPendingQueueRefresh }) {
  const { t } = useTranslation(["myJobs", "common"]);
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState("");
  const color = "#ef4444";

  async function handleRetry() {
    if (retrying) return;
    setRetrying(true);
    setActionError("");
    try {
      await retryJobChunk(backendUrl, task.job_id);
      markJobRetried(task.job_id);
      // The terminal-detail cache freezes a group's DTO on first
      // terminal entry; if a manual retry sends the group through
      // pending and back to terminal, that frozen DTO no longer reflects
      // the new sibling jobs.  Bust the cache here so the next visit
      // refetches.
      if (groupId) clearTerminalDetailFromCache(groupId);
      onRetried(task.job_id, task.chunk_index);
      // Manual retry parks a new row on pending_allocation_queue — fire
      // a one-shot pending-queue refresh now so the user sees the new
      // entry immediately, instead of waiting up to 10s for the next
      // gated poll.
      if (onPendingQueueRefresh) onPendingQueueRefresh();
      if (onRefresh) onRefresh();
    } catch (e) {
      setRetrying(false);
      setActionError(e?.message || t("failedChunks.retryFailed"));
    }
  }

  return (
    <div className="fc-cell" title={task.error || ""}>
      <div className="fc-cell-head">
        <svg
          className="fc-cell-icon"
          width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ filter: `drop-shadow(0 0 4px ${color}55)` }}
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="13" />
          <line x1="12" y1="16.5" x2="12" y2="16.51" />
        </svg>
        <span className="fc-cell-title">{rangeLabel(task)}</span>
        <button
          type="button"
          className="fc-cell-retry"
          onClick={handleRetry}
          disabled={retrying}
        >
          {retrying ? "..." : t("common:actions.retry")}
        </button>
      </div>
      {actionError && (
        <div className="inst-error" style={{ marginTop: 6 }}>{actionError}</div>
      )}
    </div>
  );
}

export default function FailedChunksPanel({ tasks, backendUrl, groupId, onRefresh, onPendingQueueRefresh }) {
  const { t } = useTranslation(["myJobs", "common"]);
  const [retriedIds, setRetriedIds] = useState(() => getRetriedJobIds());
  const [confirmation, setConfirmation] = useState("");
  const confirmTimerRef = useRef(null);

  useEffect(() => () => {
    if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
  }, []);

  function handleRetried(jobId, chunkIndex) {
    setRetriedIds((prev) => {
      const next = new Set(prev);
      next.add(jobId);
      return next;
    });
    const label = chunkIndex != null ? t("failedChunks.chunkN", { index: chunkIndex + 1 }) : t("failedChunks.chunk");
    setConfirmation(t("failedChunks.retriedConfirm", { label }));
    if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    confirmTimerRef.current = setTimeout(() => setConfirmation(""), 3000);
  }

  const retryable = (tasks || []).filter(
    (t) => t.is_retryable === true && !retriedIds.has(t.job_id),
  );
  if (retryable.length === 0 && !confirmation) return null;

  return (
    <div className="inst-panel">
      <div className="inst-panel-header">
        <div className="inst-panel-title-row">
          <div className="inst-panel-icon">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <path d="M12 9v4M12 17h.01" />
            </svg>
          </div>
          <span className="inst-panel-title">{t("failedChunks.title")}</span>
          {retryable.length > 0 && (
            <span className="inst-panel-count">{retryable.length}</span>
          )}
        </div>
      </div>

      {confirmation && (
        <div className="rentee-job-success" style={{ margin: "8px 0" }}>
          {confirmation}
        </div>
      )}

      {retryable.length > 0 && (
        <div className="fc-grid">
          {retryable.map((task) => (
            <FailedChunkCell
              key={task.job_id}
              task={task}
              backendUrl={backendUrl}
              groupId={groupId}
              onRetried={handleRetried}
              onRefresh={onRefresh}
              onPendingQueueRefresh={onPendingQueueRefresh}
            />
          ))}
        </div>
      )}
    </div>
  );
}
