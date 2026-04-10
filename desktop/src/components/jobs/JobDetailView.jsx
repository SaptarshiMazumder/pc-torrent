import {
  STATUS_LABELS,
  isTerminalStatus,
  terminalFallbackPct,
  resolveJobFilename,
  frameIndexFromFilename,
  buildAuthenticatedUrl,
  getLatestTaskWithOutput,
} from "../../utils/jobUtils";
import SegmentedProgressBar from "./SegmentedProgressBar";
import VastInstancePanel from "./VastInstancePanel";
import FrameGalleryPanel from "./FrameGalleryPanel";

function RenderGroupDetail({
  job,
  backendUrl,
  authToken,
  downloadState,
  downloadingId,
  canceling,
  galleryOpen,
  galleryState,
  openingFrameKey,
  onDownload,
  onCancel,
  onToggleGallery,
  onOpenFrame,
  onRemove,
  onReRender,
}) {
  const id = job.group_id;
  const isTerminal = isTerminalStatus(job.status);
  const rawOverallPct =
    typeof job.overall_progress_pct === "number"
      ? Math.max(0, Math.min(100, job.overall_progress_pct))
      : null;
  const overallPct = rawOverallPct !== null ? rawOverallPct : terminalFallbackPct(job.status);
  const progressLabel = overallPct !== null ? `${Math.round(overallPct)}%` : "Working...";
  const terminalStatusLabel =
    isTerminal && job.status !== "done" ? ` · ${STATUS_LABELS[job.status] || job.status}` : "";

  const tasksDone = (job.tasks || []).filter((t) => t.status === "done").length;
  const taskCount = (job.tasks || []).length;
  const availableOutputCount =
    typeof job.available_output_files_count === "number"
      ? job.available_output_files_count
      : (job.tasks || []).reduce((sum, task) => sum + (task.output_files_count || 0), 0);

  const latestTask = getLatestTaskWithOutput(job.tasks);
  const latestPreviewUrl =
    latestTask?.latest_output_file
      ? buildAuthenticatedUrl(
          backendUrl,
          `/jobs/${latestTask.job_id}/output/${encodeURIComponent(latestTask.latest_output_file)}/preview`,
          authToken,
          availableOutputCount
        )
      : "";

  const canDownloadAvailable = availableOutputCount > 0;
  const isDownloading = downloadingId === id;
  const canCancel = ["pending", "running", "uploading"].includes(job.status);
  const canViewFrames = canDownloadAvailable || canCancel || job.status === "done";

  return (
    <div className="job-detail-body">
      {(job.tasks || []).length > 0 && (
        <div className="job-detail-section">
          <div className="job-detail-section-label">Render Progress</div>
          <SegmentedProgressBar tasks={job.tasks} totalFrames={job.total_frames} />
          <div className="runtime-progress-meta" style={{ marginTop: 6 }}>
            <span>
              {typeof job.total_frames === "number"
                ? `${job.overall_rendered_frames || 0} / ${job.total_frames} frames rendered`
                : "Analyzing..."}
            </span>
            <span>
              {progressLabel}
              {terminalStatusLabel}
              {tasksDone > 0 && taskCount > 0 && (
                <> · {tasksDone}/{taskCount} machines done</>
              )}
            </span>
          </div>
        </div>
      )}

      {latestPreviewUrl && (
        <div className="job-detail-section">
          <div className="job-detail-section-label">Latest Frame</div>
          <div className="job-latest-frame-meta">
            <code>{latestTask.latest_output_file}</code>
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
        <div className="rentee-job-error">{job.error}</div>
      )}

      {job.status === "done" && (
        <div className="rentee-job-done">
          <p className="muted">
            All {taskCount} chunks complete · {availableOutputCount} frame file
            {availableOutputCount !== 1 ? "s" : ""} available
          </p>
        </div>
      )}

      {job.status !== "done" && canDownloadAvailable && (
        <div className="rentee-job-done">
          <p className="muted">
            {availableOutputCount} frame file{availableOutputCount !== 1 ? "s" : ""} available now
          </p>
        </div>
      )}

      {downloadState?.status === "done" && (
        <div className="rentee-job-success">
          Downloaded to <code>{downloadState.path}</code>
          {downloadState?.summary ? <span> ({downloadState.summary})</span> : null}
        </div>
      )}
      {downloadState?.status === "error" && (
        <div className="rentee-job-error">{downloadState.error}</div>
      )}

      <VastInstancePanel tasks={job.tasks || []} backendUrl={backendUrl} />

      <div className="job-detail-actions">
        {canCancel && (
          <button className="btn btn-danger" onClick={onCancel} disabled={canceling}>
            {canceling ? "Stopping..." : "Stop Render"}
          </button>
        )}
        {canViewFrames && (
          <button className="btn btn-secondary" onClick={onToggleGallery}>
            {galleryOpen ? "Hide Frames" : "View Frames"}
          </button>
        )}
        {canDownloadAvailable && (
          <button className="btn btn-primary" onClick={onDownload} disabled={isDownloading}>
            {isDownloading
              ? downloadState?.progress
                ? `Downloading... ${downloadState.progress}`
                : "Downloading..."
              : job.status === "done"
              ? "Download All"
              : "Download Available"}
          </button>
        )}
        <button className="btn btn-secondary" onClick={onReRender}>Re-render</button>
        <button className="btn btn-secondary" onClick={onRemove}>Remove</button>
      </div>

      {galleryOpen && (
        <FrameGalleryPanel
          id={id}
          files={galleryState?.files || []}
          loading={!!galleryState?.loading}
          error={galleryState?.error || ""}
          openingFrameKey={openingFrameKey}
          onOpenFrame={onOpenFrame}
          backendUrl={backendUrl}
        />
      )}
    </div>
  );
}

function SingleJobDetail({
  job,
  backendUrl,
  authToken,
  downloadState,
  downloadingId,
  galleryOpen,
  galleryState,
  openingFrameKey,
  onDownload,
  onToggleGallery,
  onOpenFrame,
  onRemove,
}) {
  const id = job.job_id;
  const isTerminal = isTerminalStatus(job.status);
  const totalFrames = typeof job.total_frames === "number" ? job.total_frames : null;
  const renderedFrames = typeof job.rendered_frames === "number" ? job.rendered_frames : 0;
  const rawProgressPct =
    typeof job.progress_pct === "number"
      ? Math.max(0, Math.min(100, job.progress_pct))
      : null;
  const progressPct = rawProgressPct !== null ? rawProgressPct : terminalFallbackPct(job.status);
  const progressLabel = progressPct !== null ? `${Math.round(progressPct)}%` : "Working...";
  const terminalStatusLabel =
    isTerminal && job.status !== "done" ? ` · ${STATUS_LABELS[job.status] || job.status}` : "";
  const hasTotalFrames = typeof totalFrames === "number" && totalFrames > 0;
  const showRenderProgress =
    isTerminal || job.status === "running" || hasTotalFrames || renderedFrames > 0;
  const outputFiles = Array.isArray(job.output_files) ? job.output_files : [];
  const availableOutputCount =
    typeof job.output_files_count === "number" ? job.output_files_count : outputFiles.length;
  const latestOutputFile =
    job.latest_output_file ||
    outputFiles
      .slice()
      .sort((a, b) => frameIndexFromFilename(a) - frameIndexFromFilename(b))
      .pop() ||
    "";
  const latestPreviewUrl =
    latestOutputFile && backendUrl
      ? buildAuthenticatedUrl(
          backendUrl,
          `/jobs/${id}/output/${encodeURIComponent(latestOutputFile)}/preview`,
          authToken,
          availableOutputCount
        )
      : "";
  const visibleOutputFiles = outputFiles.slice(0, 12);
  const hiddenOutputCount = Math.max(0, outputFiles.length - visibleOutputFiles.length);
  const canDownloadAvailable = availableOutputCount > 0;
  const canViewFrames = canDownloadAvailable || job.status === "running" || job.status === "pending";

  return (
    <div className="job-detail-body">
      {showRenderProgress && (
        <div className="job-detail-section">
          <div className="job-detail-section-label">Render Progress</div>
          <div className={`runtime-progress-track ${progressPct === null ? "indeterminate" : ""}`}>
            <div className="runtime-progress-fill" style={{ width: `${progressPct ?? 100}%` }} />
          </div>
          <div className="runtime-progress-meta">
            <span>
              {hasTotalFrames
                ? `${Math.min(renderedFrames, totalFrames)} / ${totalFrames} frames rendered`
                : isTerminal
                ? "Render ended"
                : "Preparing render..."}
            </span>
            <span>
              {progressLabel}
              {terminalStatusLabel}
            </span>
          </div>
        </div>
      )}

      {latestPreviewUrl && (
        <div className="job-detail-section">
          <div className="job-detail-section-label">Latest Frame</div>
          <div className="job-latest-frame-meta">
            <code>{latestOutputFile}</code>
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
        <div className="rentee-job-error">{job.error}</div>
      )}

      {job.status === "done" && (
        <div className="rentee-job-done">
          <p className="muted">{availableOutputCount} output file{availableOutputCount !== 1 ? "s" : ""}</p>
          {visibleOutputFiles.length > 0 && (
            <ul className="output-file-list">
              {visibleOutputFiles.map((f) => <li key={f}>{f}</li>)}
            </ul>
          )}
          {hiddenOutputCount > 0 && <p className="muted">+{hiddenOutputCount} more files</p>}
          {job.error && <p className="rentee-job-warning">Warning: {job.error}</p>}
        </div>
      )}

      {job.status !== "done" && canDownloadAvailable && (
        <div className="rentee-job-done">
          <p className="muted">{availableOutputCount} output file{availableOutputCount !== 1 ? "s" : ""} available now</p>
        </div>
      )}

      {downloadState?.status === "done" && (
        <div className="rentee-job-success">
          Downloaded to <code>{downloadState.path}</code>
          {downloadState?.summary ? <span> ({downloadState.summary})</span> : null}
        </div>
      )}
      {downloadState?.status === "error" && (
        <div className="rentee-job-error">{downloadState.error}</div>
      )}

      <div className="job-detail-actions">
        {canViewFrames && (
          <button className="btn btn-secondary" onClick={onToggleGallery}>
            {galleryOpen ? "Hide Frames" : "View Frames"}
          </button>
        )}
        {canDownloadAvailable && (
          <button className="btn btn-primary" onClick={onDownload} disabled={downloadingId === id}>
            {downloadingId === id
              ? "Downloading..."
              : job.status === "done"
              ? "Download"
              : "Download Available"}
          </button>
        )}
        <button className="btn btn-secondary" onClick={onRemove}>Remove</button>
      </div>

      {galleryOpen && (
        <FrameGalleryPanel
          id={id}
          files={galleryState?.files || []}
          loading={!!galleryState?.loading}
          error={galleryState?.error || ""}
          openingFrameKey={openingFrameKey}
          onOpenFrame={onOpenFrame}
          backendUrl={backendUrl}
        />
      )}
    </div>
  );
}

export default function JobDetailView({
  job,
  backendUrl,
  authToken,
  downloadState,
  downloadingId,
  canceling,
  galleryOpen,
  galleryState,
  openingFrameKey,
  onBack,
  onDownload,
  onCancel,
  onToggleGallery,
  onOpenFrame,
  onRemove,
  onReRender,
}) {
  const isGroup = !!job?.group_id;
  const displayName = resolveJobFilename(job);
  const status = job?.status || "pending";
  const id = job?.group_id || job?.job_id || "";

  return (
    <div className="job-detail">
      <div className="job-detail-header">
        <button type="button" className="job-detail-back" onClick={onBack}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M19 12H5M12 5l-7 7 7 7" />
          </svg>
          All Jobs
        </button>

        <div className="job-detail-title">
          <h2>{displayName}</h2>
          <div className="job-detail-meta-row">
            <span className={`status-badge status-${status}`}>
              {status === "running" && <span className="status-badge-dot" />}
              {STATUS_LABELS[status] || status}
            </span>
            <span className="job-detail-id">{id ? id.slice(0, 12) + "..." : ""}</span>
            {job?.submitted_at && (
              <span className="job-detail-date">
                {new Date(job.submitted_at).toLocaleString()}
              </span>
            )}
            {job?.completed_at && (
              <span className="job-detail-date">
                Completed {new Date(job.completed_at).toLocaleString()}
              </span>
            )}
          </div>
        </div>
      </div>

      {isGroup ? (
        <RenderGroupDetail
          job={job}
          backendUrl={backendUrl}
          authToken={authToken}
          downloadState={downloadState}
          downloadingId={downloadingId}
          canceling={canceling}
          galleryOpen={galleryOpen}
          galleryState={galleryState}
          openingFrameKey={openingFrameKey}
          onDownload={onDownload}
          onCancel={onCancel}
          onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame}
          onRemove={onRemove}
          onReRender={onReRender}
        />
      ) : (
        <SingleJobDetail
          job={job}
          backendUrl={backendUrl}
          authToken={authToken}
          downloadState={downloadState}
          downloadingId={downloadingId}
          galleryOpen={galleryOpen}
          galleryState={galleryState}
          openingFrameKey={openingFrameKey}
          onDownload={onDownload}
          onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame}
          onRemove={onRemove}
        />
      )}
    </div>
  );
}
