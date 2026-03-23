import { useState } from "react";
import { downloadUrl } from "../lib/api";
import { downloadJobOutputToDownloads } from "../lib/sidecar";

const STATUS_LABELS = {
  pending: "Pending",
  running: "Rendering",
  done: "Done",
  failed: "Failed",
};

export default function MyJobsPage({ jobs, removeJob, backendUrl }) {
  const [downloadingJobId, setDownloadingJobId] = useState(null);
  const [downloadResults, setDownloadResults] = useState({});

  const handleDownload = async (jobId) => {
    setDownloadResults((prev) => ({
      ...prev,
      [jobId]: { status: "loading", path: "", error: "" },
    }));
    setDownloadingJobId(jobId);

    try {
      const result = await downloadJobOutputToDownloads(downloadUrl(backendUrl, jobId));
      setDownloadResults((prev) => ({
        ...prev,
        [jobId]: { status: "done", path: result.path, error: "" },
      }));
    } catch (error) {
      setDownloadResults((prev) => ({
        ...prev,
        [jobId]: {
          status: "error",
          path: "",
          error: error.message || "Failed to download output.",
        },
      }));
    } finally {
      setDownloadingJobId(null);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>My Jobs</h2>
        <span className="log-count">{jobs.length} job{jobs.length !== 1 ? "s" : ""}</span>
      </div>

      {jobs.length === 0 ? (
        <div className="empty-state">
          <p>No jobs submitted yet.</p>
          <p className="muted">Visit the Marketplace to get started.</p>
        </div>
      ) : (
        <div className="job-list">
          {jobs.map((job) => {
            const downloadState = downloadResults[job.job_id];

            return (
              <div key={job.job_id} className="card rentee-job-card">
              <div className="rentee-job-header">
                <div className="rentee-job-info">
                  <div className="job-filename">{job.filename}</div>
                  <div className="job-id">
                    {job.machine_gpu} &middot; {job.job_id.slice(0, 8)}...
                  </div>
                </div>
                <span className={`status-badge status-${job.status}`}>
                  {job.status === "running" && (
                    <span className="status-badge-dot" />
                  )}
                  {STATUS_LABELS[job.status] || job.status}
                </span>
              </div>

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
                  {job.error && (
                    <p className="rentee-job-warning">Warning: {job.error}</p>
                  )}
                  {downloadState?.status === "done" && (
                    <div className="rentee-job-success">
                      Downloaded to
                      <code>{downloadState.path}</code>
                    </div>
                  )}
                  {downloadState?.status === "error" && (
                    <div className="rentee-job-error">{downloadState.error}</div>
                  )}
                </div>
              )}

              <div className="rentee-job-footer">
                <div className="job-meta">
                  <span>
                    Submitted{" "}
                    {new Date(job.submitted_at).toLocaleString()}
                  </span>
                  {job.completed_at && (
                    <span>
                      Completed{" "}
                      {new Date(job.completed_at).toLocaleString()}
                    </span>
                  )}
                </div>
                <div className="rentee-job-actions">
                  {job.status === "done" && (
                    <button
                      className="btn btn-primary"
                      onClick={() => handleDownload(job.job_id)}
                      disabled={downloadingJobId === job.job_id}
                    >
                      {downloadingJobId === job.job_id ? "Downloading..." : "Download"}
                    </button>
                  )}
                  <button
                    className="btn btn-secondary"
                    onClick={() => removeJob(job.job_id)}
                  >
                    Remove
                  </button>
                </div>
              </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
