import { useEffect, useRef, useState } from "react";
import { retryJobChunk } from "../../services/api";
import { getRetriedJobIds, markJobRetried } from "../../utils/retriedJobsCache";

function rangeLabel(task) {
  // Prefer the verified-upload-based remaining range — that's what will
  // actually be re-rendered.  Falls back to the original chunk range
  // only if the backend didn't provide remaining bounds.
  const start = task.remaining_frame_start ?? task.frame_start;
  const end = task.remaining_frame_end ?? task.frame_end;
  if (start == null || end == null) return null;
  return start === end ? `Frame ${start}` : `Frames ${start}-${end}`;
}

function FailedChunkRow({ task, backendUrl, onRetried, onRefresh }) {
  const [retrying, setRetrying] = useState(false);
  const [showError, setShowError] = useState(false);
  const [actionError, setActionError] = useState("");

  async function handleRetry() {
    if (retrying) return;
    setRetrying(true);
    setActionError("");
    try {
      await retryJobChunk(backendUrl, task.job_id);
      // Confirmed success: persist the retried job_id so the row stays
      // hidden across reloads, then notify the panel to drop the row
      // immediately (without waiting for the polling cycle).  Still
      // call onRefresh so the new dispatch surfaces faster than the
      // 3s poll interval.
      markJobRetried(task.job_id);
      onRetried(task.job_id, task.chunk_index);
      if (onRefresh) onRefresh();
    } catch (e) {
      setRetrying(false);
      setActionError(e?.message || "Retry failed");
    }
  }

  const range = rangeLabel(task);
  const dot = "#ef4444";

  return (
    <div className="inst-fin-row" style={{ "--inst-color": dot }}>
      <div className="inst-fin-main">
        <span className="inst-fin-dot" style={{ background: dot, boxShadow: `0 0 6px ${dot}55` }} />
        <span className="inst-fin-gpu">
          Chunk{task.chunk_index != null ? ` ${task.chunk_index + 1}` : ""}
        </span>
        <span className="inst-fin-pill" style={{ background: dot + "18", color: dot }}>
          retries exhausted
        </span>
        {range && <span className="inst-fin-stat inst-fin-range">{range}</span>}
        {task.error && (
          <button
            className="inst-fin-err-toggle"
            onClick={() => setShowError((v) => !v)}
            title="Show error"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <path d="M12 9v4M12 17h.01" />
            </svg>
          </button>
        )}
        <button
          className="btn btn-primary"
          onClick={handleRetry}
          disabled={retrying}
          style={{ marginLeft: "auto", padding: "6px 14px", fontSize: 13 }}
        >
          {retrying ? "Retrying..." : "Retry"}
        </button>
      </div>
      {showError && task.error && (
        <div className="inst-error" style={{ marginTop: 6 }}>{task.error}</div>
      )}
      {actionError && (
        <div className="inst-error" style={{ marginTop: 6 }}>{actionError}</div>
      )}
    </div>
  );
}

export default function FailedChunksPanel({ tasks, backendUrl, onRefresh }) {
  // Persisted set of job_ids the user already retried.  Read once on
  // mount; mutated locally on each successful retry so the affected row
  // re-renders out of the visible list.
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
    const label = chunkIndex != null ? `Chunk ${chunkIndex + 1}` : "Chunk";
    setConfirmation(`${label} retried — new instance will appear shortly.`);
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
          <span className="inst-panel-title">Failed Chunks</span>
          {retryable.length > 0 && (
            <span className="inst-panel-count">{retryable.length}</span>
          )}
        </div>
        {retryable.length > 0 && (
          <div className="inst-panel-chips">
            <span className="inst-chip inst-chip--failed">
              <span className="inst-chip-dot" style={{ background: "#f87171" }} />
              needs retry
            </span>
          </div>
        )}
      </div>

      {confirmation && (
        <div className="rentee-job-success" style={{ margin: "8px 0" }}>
          {confirmation}
        </div>
      )}

      {retryable.length > 0 && (
        <div className="inst-fin-list">
          {retryable.map((task) => (
            <FailedChunkRow
              key={task.job_id}
              task={task}
              backendUrl={backendUrl}
              onRetried={handleRetried}
              onRefresh={onRefresh}
            />
          ))}
        </div>
      )}
    </div>
  );
}
