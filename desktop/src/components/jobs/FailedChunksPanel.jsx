import { useState } from "react";
import { retryJobChunk } from "../../services/api";

function rangeLabel(task) {
  // Prefer the verified-upload-based remaining range — that's what will
  // actually be re-rendered.  Falls back to the original chunk range
  // only if the backend didn't provide remaining bounds.
  const start = task.remaining_frame_start ?? task.frame_start;
  const end = task.remaining_frame_end ?? task.frame_end;
  if (start == null || end == null) return null;
  return start === end ? `Frame ${start}` : `Frames ${start}-${end}`;
}

function FailedChunkRow({ task, backendUrl, onRefresh }) {
  const [retrying, setRetrying] = useState(false);
  const [showError, setShowError] = useState(false);
  const [actionError, setActionError] = useState("");

  async function handleRetry() {
    if (retrying) return;
    setRetrying(true);
    setActionError("");
    try {
      await retryJobChunk(backendUrl, task.job_id);
      // Server flipped the group from 'failed' back to 'running' inside
      // retry_chunk_manually -> reconcile_group_status.  Force a refetch
      // so the local cached status updates from 'failed' to 'running' --
      // otherwise useJobs's polling filter (which excludes terminal
      // groups) never refreshes this view and the new dispatched
      // instance never appears.
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
  const retryable = (tasks || []).filter((t) => t.is_retryable === true);
  if (retryable.length === 0) return null;

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
          <span className="inst-panel-count">{retryable.length}</span>
        </div>
        <div className="inst-panel-chips">
          <span className="inst-chip inst-chip--failed">
            <span className="inst-chip-dot" style={{ background: "#f87171" }} />
            needs retry
          </span>
        </div>
      </div>

      <div className="inst-fin-list">
        {retryable.map((task) => (
          <FailedChunkRow key={task.job_id} task={task} backendUrl={backendUrl} onRefresh={onRefresh} />
        ))}
      </div>
    </div>
  );
}
