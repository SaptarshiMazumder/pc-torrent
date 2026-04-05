import { useState } from "react";
import { cancelRenderGroup, getRenderGroupOutputs, getJobOutputs, jobOutputsUrl, renderGroupOutputsUrl } from "../lib/api";
import { downloadJobOutputToDownloads } from "../lib/sidecar";
import SegmentedProgressBar from "../components/SegmentedProgressBar";

const STATUS_LABELS = {
  pending: "Pending",
  uploading: "Uploading",
  running: "Rendering",
  done: "Done",
  cancelled: "Cancelled",
  failed: "Failed",
};

function buildDownloadFolderName(jobFilename, id) {
  const rawName = typeof jobFilename === "string" && jobFilename.trim()
    ? jobFilename.trim()
    : "render";
  const baseName = rawName.replace(/\.[^/.]+$/, "");
  const suffix = typeof id === "string" && id ? id.slice(0, 8) : "job";
  return `render__${baseName}__${suffix}`;
}

function frameIndexFromFilename(filename) {
  if (typeof filename !== "string") return -1;
  const match = filename.match(/(\d+)(?=\.[^.]+$)/);
  if (!match) return -1;
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : -1;
}

export default function MyJobsPage({ jobs, removeJob, backendUrl, markRenderGroupCancelled }) {
  const [downloadingId, setDownloadingId] = useState(null);
  const [downloadResults, setDownloadResults] = useState({});
  const [cancelingGroupIds, setCancelingGroupIds] = useState({});

  const handleDownload = async (id, fetchOutputs, jobFilename) => {
    setDownloadResults((prev) => ({
      ...prev,
      [id]: { status: "loading", path: "", error: "", progress: "" },
    }));
    setDownloadingId(id);
    try {
      const jobFolder = buildDownloadFolderName(jobFilename, id);
      // Fetch the list of presigned R2 URLs — no file data passes through the server
      const data = await fetchOutputs();

      const files = data.files || [];
      if (files.length === 0) throw new Error("No output files found");

      let lastPath = "";
      for (let i = 0; i < files.length; i++) {
        const { filename, url: fileUrl } = files[i];
        setDownloadResults((prev) => ({
          ...prev,
          [id]: { status: "loading", path: "", error: "", progress: `${i + 1} / ${files.length}` },
        }));
        const result = await downloadJobOutputToDownloads(fileUrl, {
          jobFolder,
          preferredFilename: filename,
        });
        lastPath = result.path.replace(/[^\\/]+$/, ""); // folder path
      }

      setDownloadResults((prev) => ({
        ...prev,
        [id]: { status: "done", path: lastPath || "Downloads", error: "", progress: "" },
      }));
    } catch (error) {
      setDownloadResults((prev) => ({
        ...prev,
        [id]: { status: "error", path: "", error: error.message || "Download failed.", progress: "" },
      }));
    } finally {
      setDownloadingId(null);
    }
  };

  const handleCancelRenderGroup = async (groupId) => {
    if (!groupId || cancelingGroupIds[groupId]) return;
    setCancelingGroupIds((prev) => ({ ...prev, [groupId]: true }));
    try {
      await cancelRenderGroup(backendUrl, groupId);
      markRenderGroupCancelled?.(groupId);
    } catch (error) {
      const message = error?.message || "Failed to cancel render group";
      setDownloadResults((prev) => ({
        ...prev,
        [groupId]: { status: "error", path: "", error: message, progress: "" },
      }));
    } finally {
      setCancelingGroupIds((prev) => {
        const next = { ...prev };
        delete next[groupId];
        return next;
      });
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
                  backendUrl={backendUrl}
                  downloadState={downloadState}
                  downloadingId={downloadingId}
                  canceling={!!cancelingGroupIds[id]}
                  onDownload={() =>
                    handleDownload(id, () => getRenderGroupOutputs(backendUrl, id), job.filename)
                  }
                  onCancel={() => {
                    void handleCancelRenderGroup(id);
                  }}
                  onRemove={() => removeJob(id)}
                />
              );
            }

            // Legacy single-machine job
            return (
              <SingleJobCard
                key={id}
                job={job}
                backendUrl={backendUrl}
                downloadState={downloadState}
                downloadingId={downloadingId}
                onDownload={() =>
                  handleDownload(id, () => getJobOutputs(backendUrl, id), job.filename)
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

function RenderGroupCard({
  job,
  backendUrl,
  downloadState,
  downloadingId,
  canceling,
  onDownload,
  onCancel,
  onRemove,
}) {
  const id = job.group_id;
  const overallPct =
    job.status === "done"
      ? 100
      : typeof job.overall_progress_pct === "number"
      ? Math.max(0, Math.min(100, job.overall_progress_pct))
      : null;

  const tasksDone = (job.tasks || []).filter((t) => t.status === "done").length;
  const taskCount = (job.tasks || []).length;
  const availableOutputCount =
    typeof job.available_output_files_count === "number"
      ? job.available_output_files_count
      : (job.tasks || []).reduce((sum, task) => sum + (task.output_files_count || 0), 0);
  const latestTaskWithOutput = (job.tasks || []).reduce((best, task) => {
    if (!task?.latest_output_file) return best;
    if (!best?.latest_output_file) return task;
    const bestFrame = frameIndexFromFilename(best.latest_output_file);
    const taskFrame = frameIndexFromFilename(task.latest_output_file);
    if (taskFrame > bestFrame) return task;
    if (taskFrame === bestFrame && task.latest_output_file > best.latest_output_file) return task;
    return best;
  }, null);
  const latestPreviewUrl =
    latestTaskWithOutput && latestTaskWithOutput.latest_output_file
      ? `${backendUrl}/jobs/${latestTaskWithOutput.job_id}/output/${encodeURIComponent(
          latestTaskWithOutput.latest_output_file
        )}?v=${availableOutputCount}`
      : "";
  const canDownloadAvailable = availableOutputCount > 0;
  const isDownloading = downloadingId === id;
  const canCancel = ["pending", "running", "uploading"].includes(job.status);

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

      {latestPreviewUrl && (
        <div className="job-latest-frame-wrap">
          <div className="job-latest-frame-meta">
            Latest frame: <code>{latestTaskWithOutput.latest_output_file}</code>
          </div>
          <img
            className="job-latest-frame-preview"
            src={latestPreviewUrl}
            alt="Latest rendered frame preview"
            loading="lazy"
          />
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
            All {taskCount} chunks complete &middot; {availableOutputCount} frame file
            {availableOutputCount !== 1 ? "s" : ""} available
          </p>
        </div>
      )}

      {job.status !== "done" && canDownloadAvailable && (
        <div className="rentee-job-done">
          <p className="muted">
            {availableOutputCount} frame file{availableOutputCount !== 1 ? "s" : ""} already available now
          </p>
        </div>
      )}

      {downloadState?.status === "done" && (
        <div className="rentee-job-success">
          Downloaded to <code>{downloadState.path}</code>
        </div>
      )}
      {downloadState?.status === "error" && (
        <div className="rentee-job-error">{downloadState.error}</div>
      )}

      <div className="rentee-job-footer">
        <div className="job-meta">
          <span>Submitted {new Date(job.submitted_at).toLocaleString()}</span>
          {job.completed_at && (
            <span>Completed {new Date(job.completed_at).toLocaleString()}</span>
          )}
        </div>
        <div className="rentee-job-actions">
          {canCancel && (
            <button
              className="btn btn-danger"
              onClick={onCancel}
              disabled={canceling}
            >
              {canceling ? "Stopping..." : "Stop Render"}
            </button>
          )}
          {canDownloadAvailable && (
            <button
              className="btn btn-primary"
              onClick={onDownload}
              disabled={isDownloading}
            >
              {isDownloading
                ? downloadState?.progress
                  ? `Downloading... ${downloadState.progress}`
                  : "Downloading..."
                : job.status === "done"
                ? "Download All"
                : "Download Available"}
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

function SingleJobCard({ job, backendUrl, downloadState, downloadingId, onDownload, onRemove }) {
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
  const outputFiles = Array.isArray(job.output_files) ? job.output_files : [];
  const availableOutputCount =
    typeof job.output_files_count === "number" ? job.output_files_count : outputFiles.length;
  const latestOutputFile =
    job.latest_output_file || outputFiles.slice().sort((a, b) => frameIndexFromFilename(a) - frameIndexFromFilename(b)).pop() || "";
  const latestPreviewUrl =
    latestOutputFile && backendUrl
      ? `${backendUrl}/jobs/${id}/output/${encodeURIComponent(latestOutputFile)}?v=${availableOutputCount}`
      : "";
  const visibleOutputFiles = outputFiles.slice(0, 12);
  const hiddenOutputCount = Math.max(0, outputFiles.length - visibleOutputFiles.length);
  const canDownloadAvailable = availableOutputCount > 0;

  return (
    <div className="card rentee-job-card">
      <div className="rentee-job-header">
        <div className="rentee-job-info">
          <div className="job-filename">{job.filename}</div>
          <div className="job-id">
            {job.machine_gpu} &middot; {id?.slice(0, 8)}...
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

      {latestPreviewUrl && (
        <div className="job-latest-frame-wrap">
          <div className="job-latest-frame-meta">
            Latest frame: <code>{latestOutputFile}</code>
          </div>
          <img
            className="job-latest-frame-preview"
            src={latestPreviewUrl}
            alt="Latest rendered frame preview"
            loading="lazy"
          />
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
            {availableOutputCount} output file
            {availableOutputCount !== 1 ? "s" : ""}
          </p>
          {visibleOutputFiles.length > 0 && (
            <ul className="output-file-list">
              {visibleOutputFiles.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
          {hiddenOutputCount > 0 && (
            <p className="muted">+{hiddenOutputCount} more files</p>
          )}
          {job.error && <p className="rentee-job-warning">Warning: {job.error}</p>}
        </div>
      )}

      {job.status !== "done" && canDownloadAvailable && (
        <div className="rentee-job-done">
          <p className="muted">
            {availableOutputCount} output file{availableOutputCount !== 1 ? "s" : ""} available now
          </p>
        </div>
      )}

      {downloadState?.status === "done" && (
        <div className="rentee-job-success">
          Downloaded to <code>{downloadState.path}</code>
        </div>
      )}
      {downloadState?.status === "error" && (
        <div className="rentee-job-error">{downloadState.error}</div>
      )}

      <div className="rentee-job-footer">
        <div className="job-meta">
          <span>Submitted {new Date(job.submitted_at).toLocaleString()}</span>
          {job.completed_at && (
            <span>Completed {new Date(job.completed_at).toLocaleString()}</span>
          )}
        </div>
        <div className="rentee-job-actions">
          {canDownloadAvailable && (
            <button
              className="btn btn-primary"
              onClick={onDownload}
              disabled={downloadingId === id}
            >
              {downloadingId === id
                ? "Downloading..."
                : job.status === "done"
                ? "Download"
                : "Download Available"}
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
