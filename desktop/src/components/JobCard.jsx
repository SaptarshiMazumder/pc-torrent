import { pauseAgent, resumeAgent, stopJob } from "../lib/sidecar";

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
