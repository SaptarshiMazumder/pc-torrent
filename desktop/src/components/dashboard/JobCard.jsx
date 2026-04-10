import { pauseAgent, resumeAgent, stopJob } from "../../services/sidecar";

export default function JobCard({ currentJob, status }) {
  if (!currentJob) {
    if (status === "connected") {
      return (
        <div className="card job-card">
          <h3>Current Job</h3>
          <p className="muted">Waiting for render jobs...</p>
        </div>
      );
    }
    return null;
  }

  const totalFrames =
    typeof currentJob.total_frames === "number" ? currentJob.total_frames : null;
  const renderedFrames =
    typeof currentJob.rendered_frames === "number" ? currentJob.rendered_frames : 0;
  const progressPct =
    typeof currentJob.progress_pct === "number"
      ? Math.max(0, Math.min(100, currentJob.progress_pct))
      : null;
  const hasTotalFrames = typeof totalFrames === "number" && totalFrames > 0;

  return (
    <div className="card job-card active">
      <h3>Current Job</h3>
      <div className="job-info">
        <div className="job-filename">{currentJob.filename}</div>
        <div className="job-id">ID: {currentJob.job_id}</div>
        <div className="job-status">
          <span className="rendering-indicator" />
          Rendering...
        </div>
      </div>
      <div className="job-progress-wrap">
        <div className={`runtime-progress-track ${progressPct === null ? "indeterminate" : ""}`}>
          <div
            className="runtime-progress-fill"
            style={{ width: `${progressPct ?? 100}%` }}
          />
        </div>
        <div className="runtime-progress-meta">
          <span>
            {hasTotalFrames
              ? `${Math.min(renderedFrames, totalFrames)} / ${totalFrames} frames`
              : "Preparing render..."}
          </span>
          <span>{progressPct !== null ? `${Math.round(progressPct)}%` : "Working..."}</span>
        </div>
        {typeof currentJob.current_frame === "number" && (
          <div className="job-progress-caption">
            Current frame: {currentJob.current_frame}
          </div>
        )}
      </div>
      <div className="job-actions">
        {status === "paused" ? (
          <button className="btn btn-secondary" onClick={resumeAgent}>
            Resume
          </button>
        ) : (
          <button className="btn btn-secondary" onClick={pauseAgent}>
            Pause
          </button>
        )}
        <button className="btn btn-danger" onClick={stopJob}>
          Stop
        </button>
      </div>
    </div>
  );
}
