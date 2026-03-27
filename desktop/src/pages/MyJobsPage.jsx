import { useState } from "react";
import { downloadUrl, renderGroupDownloadUrl } from "../lib/api";
import { downloadJobOutputToDownloads } from "../lib/sidecar";
import SegmentedProgressBar from "../components/SegmentedProgressBar";

const STATUS_LABELS = {
  pending: "Pending",
  running: "Rendering",
  done: "Done",
  failed: "Failed",
};

export default function MyJobsPage({ jobs, removeJob, backendUrl }) {
  const [downloadingId, setDownloadingId] = useState(null);
  const [downloadResults, setDownloadResults] = useState({});

  const handleDownload = async (id, url) => {
    setDownloadResults((prev) => ({
      ...prev,
      [id]: { status: "loading", path: "", error: "" },
    }));
    setDownloadingId(id);
    try {
      const result = await downloadJobOutputToDownloads(url);
      setDownloadResults((prev) => ({
        ...prev,
        [id]: { status: "done", path: result.path, error: "" },
      }));
    } catch (error) {
      setDownloadResults((prev) => ({
        ...prev,
        [id]: { status: "error", path: "", error: error.message || "Download failed." },
      }));
    } finally {
      setDownloadingId(null);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>My Jobs</h2>
        <span className="log-count">
          {jobs.length} job{jobs.length !== 1 ? "s" : ""}
        </span>
      </div>

      {jobs.length === 0 ? (
        <div className="empty-state">
          <p>No jobs submitted yet.</p>
          <p className="muted">Visit the Marketplace to get started.</p>
        </div>
      ) : (
        <div className="job-list">
          {jobs.map((job) => {
            const isGroup = !!job.group_id;
            const id = isGroup ? job.group_id : job.job_id;
            const downloadState = downloadResults[id];

            if (isGroup) {
              return (
                <RenderGroupCard
                  key={id}
                  job={job}
                  downloadState={downloadState}
                  downloadingId={downloadingId}
                  onDownload={() =>
                    handleDownload(id, renderGroupDownloadUrl(backendUrl, id))
                  }
                  onRemove={() => removeJob(id)}
                />
              );
            }

            // Legacy single-machine job
            return (
              <SingleJobCard
                key={id}
                job={job}
                downloadState={downloadState}
                downloadingId={downloadingId}
                onDownload={() =>
                  handleDownload(id, downloadUrl(backendUrl, id))
                }
                onRemove={() => removeJob(id)}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function RenderGroupCard({ job, downloadState, downloadingId, onDownload, onRemove }) {
  const id = job.group_id;
  const overallPct =
    job.status === "done"
      ? 100
      : typeof job.overall_progress_pct === "number"
      ? Math.max(0, Math.min(100, job.overall_progress_pct))
      : null;

  const tasksDone = (job.tasks || []).filter((t) => t.status === "done").length;
  const taskCount = (job.tasks || []).length;

  return (
    <div className="card rentee-job-card">
      <div className="rentee-job-header">
        <div className="rentee-job-info">
          <div className="job-filename">{job.filename}</div>
          <div className="job-id">
            {taskCount} machine{taskCount !== 1 ? "s" : ""} &middot; {id.slice(0, 8)}...
          </div>
        </div>
        <span className={`status-badge status-${job.status}`}>
          {job.status === "running" && <span className="status-badge-dot" />}
          {STATUS_LABELS[job.status] || job.status}
        </span>
      </div>

      {/* Torrent-style segmented progress */}
      {(job.tasks || []).length > 0 && (
        <div className="rentee-job-progress">
          <SegmentedProgressBar tasks={job.tasks} totalFrames={job.total_frames} />
          <div className="runtime-progress-meta" style={{ marginTop: 6 }}>
            <span>
              {typeof job.total_frames === "number"
                ? `${job.overall_rendered_frames || 0} / ${job.total_frames} frames rendered`
                : "Analyzing..."}
            </span>
            <span>
              {overallPct !== null ? `${Math.round(overallPct)}%` : "Working..."}
              {tasksDone > 0 && taskCount > 0 && (
                <> &middot; {tasksDone}/{taskCount} machines done</>
              )}
            </span>
          </div>
        </div>
      )}

      {job.status === "failed" && job.error && (
        <div className="rentee-job-error">
          <strong>Error:</strong> {job.error}
        </div>
      )}

      {job.status === "done" && (
        <div className="rentee-job-done">
          <p className="muted">All {taskCount} chunks complete</p>
          {downloadState?.status === "done" && (
            <div className="rentee-job-success">
              Downloaded to <code>{downloadState.path}</code>
            </div>
          )}
          {downloadState?.status === "error" && (
            <div className="rentee-job-error">{downloadState.error}</div>
          )}
        </div>
      )}

      <div className="rentee-job-footer">
        <div className="job-meta">
          <span>Submitted {new Date(job.submitted_at).toLocaleString()}</span>
          {job.completed_at && (
            <span>Completed {new Date(job.completed_at).toLocaleString()}</span>
          )}
        </div>
        <div className="rentee-job-actions">
          {job.status === "done" && (
            <button
              className="btn btn-primary"
              onClick={onDownload}
              disabled={downloadingId === id}
            >
              {downloadingId === id ? "Downloading..." : "Download All"}
            </button>
          )}
          <button className="btn btn-secondary" onClick={onRemove}>
            Remove
          </button>
        </div>
      </div>
    </div>
  );
}

function SingleJobCard({ job, downloadState, downloadingId, onDownload, onRemove }) {
  const id = job.job_id;
  const totalFrames =
    typeof job.total_frames === "number" ? job.total_frames : null;
  const renderedFrames =
    typeof job.rendered_frames === "number" ? job.rendered_frames : 0;
  const progressPct =
    typeof job.progress_pct === "number"
      ? Math.max(0, Math.min(100, job.progress_pct))
      : null;
  const hasTotalFrames = typeof totalFrames === "number" && totalFrames > 0;
  const showRenderProgress =
    job.status === "running" || hasTotalFrames || renderedFrames > 0;

  return (
    <div className="card rentee-job-card">
      <div className="rentee-job-header">
        <div className="rentee-job-info">
          <div className="job-filename">{job.filename}</div>
          <div className="job-id">
            {job.machine_gpu} &middot; {id.slice(0, 8)}...
          </div>
        </div>
        <span className={`status-badge status-${job.status}`}>
          {job.status === "running" && <span className="status-badge-dot" />}
          {STATUS_LABELS[job.status] || job.status}
        </span>
      </div>

      {showRenderProgress && (
        <div className="rentee-job-progress">
          <div className={`runtime-progress-track ${progressPct === null ? "indeterminate" : ""}`}>
            <div
              className="runtime-progress-fill"
              style={{ width: `${progressPct ?? 100}%` }}
            />
          </div>
          <div className="runtime-progress-meta">
            <span>
              {hasTotalFrames
                ? `${Math.min(renderedFrames, totalFrames)} / ${totalFrames} frames rendered`
                : "Preparing render..."}
            </span>
            <span>
              {progressPct !== null ? `${Math.round(progressPct)}%` : "Working..."}
            </span>
          </div>
        </div>
      )}

      {job.status === "failed" && job.error && (
        <div className="rentee-job-error">
          <strong>Error:</strong> {job.error}
        </div>
      )}

      {job.status === "done" && (
        <div className="rentee-job-done">
          <p className="muted">
            {job.output_files.length} output file
            {job.output_files.length !== 1 ? "s" : ""}
          </p>
          {job.output_files.length > 0 && (
            <ul className="output-file-list">
              {job.output_files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
          {job.error && <p className="rentee-job-warning">Warning: {job.error}</p>}
          {downloadState?.status === "done" && (
            <div className="rentee-job-success">
              Downloaded to <code>{downloadState.path}</code>
            </div>
          )}
          {downloadState?.status === "error" && (
            <div className="rentee-job-error">{downloadState.error}</div>
          )}
        </div>
      )}

      <div className="rentee-job-footer">
        <div className="job-meta">
          <span>Submitted {new Date(job.submitted_at).toLocaleString()}</span>
          {job.completed_at && (
            <span>Completed {new Date(job.completed_at).toLocaleString()}</span>
          )}
        </div>
        <div className="rentee-job-actions">
          {job.status === "done" && (
            <button
              className="btn btn-primary"
              onClick={onDownload}
              disabled={downloadingId === id}
            >
              {downloadingId === id ? "Downloading..." : "Download"}
            </button>
          )}
          <button className="btn btn-secondary" onClick={onRemove}>
            Remove
          </button>
        </div>
      </div>
    </div>
  );
}
