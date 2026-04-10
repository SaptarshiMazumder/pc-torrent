import { useCallback, useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import {
  cancelRenderGroup,
  getFirebaseToken,
  getRenderGroupOutputs,
  getJobOutputs,
} from "../lib/api";
import { downloadJobOutputToDownloads, cacheViewerFrame } from "../lib/sidecar";
import SegmentedProgressBar from "../components/SegmentedProgressBar";
import FrameThumb from "../components/FrameThumb";
import VastInstancePanel from "../components/VastInstancePanel";

const STATUS_LABELS = {
  pending: "Pending",
  uploading: "Uploading",
  running: "Rendering",
  done: "Done",
  cancelled: "Cancelled",
  failed: "Failed",
};
const TERMINAL_STATUSES = new Set(["done", "cancelled", "failed"]);

function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(status);
}

function terminalFallbackPct(status) {
  if (status === "done") return 100;
  if (status === "cancelled" || status === "failed") return 0;
  return null;
}

function resolveJobFilename(job) {
  if (typeof job?.filename === "string" && job.filename.trim()) return job.filename.trim();
  if (typeof job?.input_filename === "string" && job.input_filename.trim()) return job.input_filename.trim();
  return "Untitled";
}

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

function outputSort(a, b) {
  const frameA = frameIndexFromFilename(a?.filename || "");
  const frameB = frameIndexFromFilename(b?.filename || "");
  if (frameA !== frameB) return frameA - frameB;
  return String(a?.filename || "").localeCompare(String(b?.filename || ""));
}

function jobKey(job) {
  return job?.group_id || job?.job_id || "";
}

function buildAuthenticatedUrl(baseUrl, path, token, cacheBuster = null) {
  if (!baseUrl || !path) return "";
  const normalizedBase = String(baseUrl).trim().replace(/\/+$/, "");
  const url = new URL(path, `${normalizedBase}/`);
  if (token) {
    url.searchParams.set("token", token);
  }
  if (cacheBuster !== null && cacheBuster !== undefined) {
    url.searchParams.set("v", String(cacheBuster));
  }
  return url.toString();
}

function summarizeDownloadActions(actions) {
  const parts = [];
  if (actions.downloaded) parts.push(`${actions.downloaded} downloaded`);
  if (actions.skipped) parts.push(`${actions.skipped} already had`);
  if (actions.failed) parts.push(`${actions.failed} failed`);
  return parts.join(", ") || "done";
}

// Persists download results across page navigations (component unmounts/remounts)
const _downloadResultsStore = {};

export default function MyJobsPage({ jobs, loading, removeJob, backendUrl, markRenderGroupCancelled, onRefresh }) {
  const [downloadingId, setDownloadingId] = useState(null);
  const [downloadResults, _setDownloadResults] = useState(() => ({ ..._downloadResultsStore }));

  // Wrap setter to also write through to the module-level store
  const setDownloadResults = useCallback((updater) => {
    _setDownloadResults((prev) => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      // Sync to persistent store
      Object.keys(_downloadResultsStore).forEach((k) => delete _downloadResultsStore[k]);
      Object.assign(_downloadResultsStore, next);
      return next;
    });
  }, []);
  const [cancelingGroupIds, setCancelingGroupIds] = useState({});
  const [openFrameGalleries, setOpenFrameGalleries] = useState({});
  const [frameGalleries, setFrameGalleries] = useState({});
  const [authToken, setAuthToken] = useState("");
  const [openingFrameKey, setOpeningFrameKey] = useState("");
  const [frameViewer, setFrameViewer] = useState(null);
  const jobsRef = useRef(jobs);

  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  useEffect(() => {
    let cancelled = false;

    const refreshToken = async () => {
      try {
        const token = await getFirebaseToken();
        if (!cancelled) {
          setAuthToken(token || "");
        }
      } catch {
        if (!cancelled) {
          setAuthToken("");
        }
      }
    };

    void refreshToken();
    const timer = setInterval(() => {
      void refreshToken();
    }, 10 * 60 * 1000);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const handleDownload = async (id, fetchOutputs, jobFilename) => {
    setDownloadResults((prev) => ({
      ...prev,
      [id]: { status: "loading", path: "", error: "", progress: "", summary: "" },
    }));
    setDownloadingId(id);
    try {
      const jobFolder = buildDownloadFolderName(jobFilename, id);
      // Fetch the list of presigned R2 URLs — no file data passes through the server
      const data = await fetchOutputs();

      const files = Array.isArray(data?.files) ? data.files : [];
      if (files.length === 0) throw new Error("No output files found");

      let lastPath = "";
      const stats = { downloaded: 0, skipped: 0, failed: 0 };
      for (let i = 0; i < files.length; i++) {
        const { filename, url: fileUrl, size_bytes: sizeBytes } = files[i];
        setDownloadResults((prev) => ({
          ...prev,
          [id]: {
            status: "loading",
            path: "",
            error: "",
            progress: `${i + 1} / ${files.length}`,
            summary: summarizeDownloadActions(stats),
          },
        }));
        try {
          const result = await downloadJobOutputToDownloads(fileUrl, {
            jobFolder,
            preferredFilename: filename,
            expectedSizeBytes: Number.isFinite(sizeBytes) ? sizeBytes : null,
            overwriteExisting: false, // skip if already downloaded with matching size
          });
          const action = String(result?.action || "downloaded");
          if (action === "skipped") stats.skipped += 1;
          else stats.downloaded += 1;
          if (result?.path) lastPath = result.path.replace(/[^\\/]+$/, "");
        } catch {
          stats.failed += 1;
        }
      }

      setDownloadResults((prev) => ({
        ...prev,
        [id]: {
          status: "done",
          path: lastPath || "Downloads",
          error: "",
          progress: "",
          summary: summarizeDownloadActions(stats),
        },
      }));
    } catch (error) {
      setDownloadResults((prev) => ({
        ...prev,
        [id]: { status: "error", path: "", error: error.message || "Download failed.", progress: "", summary: "" },
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

  const fetchFrameGallery = useCallback(
    async (job, { silent = false } = {}) => {
      const id = jobKey(job);
      if (!id) return;

      if (!silent) {
        setFrameGalleries((prev) => ({
          ...prev,
          [id]: {
            ...prev[id],
            loading: true,
            error: "",
            files: prev[id]?.files || [],
          },
        }));
      }

      try {
        const payload = job.group_id
          ? await getRenderGroupOutputs(backendUrl, id)
          : await getJobOutputs(backendUrl, id);
        const files = Array.isArray(payload?.files)
          ? payload.files.slice().sort(outputSort)
          : [];
        setFrameGalleries((prev) => ({
          ...prev,
          [id]: {
            loading: false,
            error: "",
            files,
            updatedAt: Date.now(),
          },
        }));
      } catch (err) {
        setFrameGalleries((prev) => ({
          ...prev,
          [id]: {
            loading: false,
            error: err?.message || "Failed to load frames",
            files: prev[id]?.files || [],
            updatedAt: Date.now(),
          },
        }));
      }
    },
    [backendUrl, authToken]
  );

  const handleOpenFrame = useCallback(async (job, file) => {
    const id = jobKey(job);
    if (!id || !file?.url || !file?.filename) return;
    const fileKey = `${id}:${file.job_id || ""}:${file.filename}`;
    // Cache key scoped to job so same filename across jobs doesn't collide
    const cacheKey = `${id}_${file.job_id || ""}_${file.filename}`;
    setOpeningFrameKey(fileKey);
    setFrameViewer({
      title: file.filename,
      loading: true,
      error: "",
      imageSrc: "",
      localPath: "",
      action: "",
    });
    try {
      const localPath = await cacheViewerFrame(
        file.url,
        cacheKey,
        Number.isFinite(file.size_bytes) ? file.size_bytes : null,
      );
      setFrameViewer({
        title: file.filename,
        loading: false,
        error: "",
        imageSrc: convertFileSrc(localPath),
        localPath,
        action: "cached",
      });
    } catch (error) {
      setFrameViewer({
        title: file.filename,
        loading: false,
        error: error?.message || "Failed to load frame",
        imageSrc: "",
        localPath: "",
        action: "",
      });
    } finally {
      setOpeningFrameKey("");
    }
  }, []);

  const handleToggleFrameGallery = useCallback(
    (job) => {
      const id = jobKey(job);
      if (!id) return;
      setOpenFrameGalleries((prev) => {
        const isOpen = !!prev[id];
        const next = { ...prev, [id]: !isOpen };
        if (isOpen) {
          delete next[id];
        }
        return next;
      });
      if (!openFrameGalleries[id]) {
        void fetchFrameGallery(job, { silent: false });
      }
    },
    [fetchFrameGallery, openFrameGalleries]
  );

  useEffect(() => {
    const openIds = Object.keys(openFrameGalleries).filter((id) => openFrameGalleries[id]);
    if (openIds.length === 0) return;

    const tick = () => {
      const currentJobs = jobsRef.current || [];
      for (const id of openIds) {
        const job = currentJobs.find((candidate) => jobKey(candidate) === id);
        if (job) {
          void fetchFrameGallery(job, { silent: true });
        }
      }
    };

    tick();
    const timer = setInterval(tick, 2500);
    return () => clearInterval(timer);
  }, [openFrameGalleries, fetchFrameGallery]);

  return (
    <div className="page">
      <div className="page-header">
        <h2>My Jobs</h2>
        <span className="log-count">
          {jobs.length} job{jobs.length !== 1 ? "s" : ""}
        </span>
        <button
          className="btn btn-secondary"
          type="button"
          onClick={onRefresh}
          disabled={loading}
          style={{ marginLeft: "auto" }}
        >
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {jobs.length === 0 ? (
        <div className="empty-state">
          <p>No jobs submitted yet.</p>
          <p className="muted">Go to Create Render to start your first job.</p>
        </div>
      ) : (
        <div className="job-list">
          {jobs.map((job) => {
            const isGroup = !!job.group_id;
            const id = isGroup ? job.group_id : job.job_id;
            if (!id) return null;
            const downloadState = downloadResults[id];
            const displayName = resolveJobFilename(job);

            if (isGroup) {
              return (
                <RenderGroupCard
                  key={id}
                  job={job}
                  backendUrl={backendUrl}
                  downloadState={downloadState}
                  downloadingId={downloadingId}
                  canceling={!!cancelingGroupIds[id]}
                  authToken={authToken}
                  galleryOpen={!!openFrameGalleries[id]}
                  galleryState={frameGalleries[id]}
                  openingFrameKey={openingFrameKey}
                  onDownload={() =>
                    handleDownload(id, () => getRenderGroupOutputs(backendUrl, id), displayName)
                  }
                  onCancel={() => {
                    void handleCancelRenderGroup(id);
                  }}
                  onToggleGallery={() => {
                    handleToggleFrameGallery(job);
                  }}
                  onOpenFrame={(file) => {
                    void handleOpenFrame(job, file);
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
                authToken={authToken}
                galleryOpen={!!openFrameGalleries[id]}
                galleryState={frameGalleries[id]}
                openingFrameKey={openingFrameKey}
                onDownload={() =>
                  handleDownload(id, () => getJobOutputs(backendUrl, id), displayName)
                }
                onToggleGallery={() => {
                  handleToggleFrameGallery(job);
                }}
                onOpenFrame={(file) => {
                  void handleOpenFrame(job, file);
                }}
                onRemove={() => removeJob(id)}
              />
            );
          })}
        </div>
      )}

      {frameViewer && (
        <FrameViewerModal
          viewer={frameViewer}
          onClose={() => setFrameViewer(null)}
        />
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
  authToken,
  galleryOpen,
  galleryState,
  openingFrameKey,
  onDownload,
  onCancel,
  onToggleGallery,
  onOpenFrame,
  onRemove,
}) {
  const id = job.group_id;
  const isTerminal = isTerminalStatus(job.status);
  const displayName = resolveJobFilename(job);
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
      ? buildAuthenticatedUrl(
          backendUrl,
          `/jobs/${latestTaskWithOutput.job_id}/output/${encodeURIComponent(
            latestTaskWithOutput.latest_output_file
          )}/preview`,
          authToken,
          availableOutputCount
        )
      : "";
  const canDownloadAvailable = availableOutputCount > 0;
  const isDownloading = downloadingId === id;
  const canCancel = ["pending", "running", "uploading"].includes(job.status);
  const canViewFrames = canDownloadAvailable || canCancel || job.status === "done";

  return (
    <div className="card rentee-job-card">
      <div className="rentee-job-header">
        <div className="rentee-job-info">
          <div className="job-filename">{displayName}</div>
          <div className="job-id">
            {taskCount} machine{taskCount !== 1 ? "s" : ""} &middot; {id ? `${id.slice(0, 8)}...` : "unknown"}
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
              {progressLabel}
              {terminalStatusLabel}
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
          {downloadState?.summary ? <span>({downloadState.summary})</span> : null}
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
          {canViewFrames && (
            <button className="btn btn-secondary" onClick={onToggleGallery}>
              {galleryOpen ? "Hide Frames" : "View Frames"}
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

      {/* Vast.ai live instance monitoring */}
      <VastInstancePanel tasks={job.tasks || []} backendUrl={backendUrl} />

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

function SingleJobCard({
  job,
  backendUrl,
  downloadState,
  downloadingId,
  authToken,
  galleryOpen,
  galleryState,
  openingFrameKey,
  onDownload,
  onToggleGallery,
  onOpenFrame,
  onRemove,
}) {
  const id = job.job_id;
  const displayName = resolveJobFilename(job);
  const isTerminal = isTerminalStatus(job.status);
  const totalFrames =
    typeof job.total_frames === "number" ? job.total_frames : null;
  const renderedFrames =
    typeof job.rendered_frames === "number" ? job.rendered_frames : 0;
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
    job.latest_output_file || outputFiles.slice().sort((a, b) => frameIndexFromFilename(a) - frameIndexFromFilename(b)).pop() || "";
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
    <div className="card rentee-job-card">
      <div className="rentee-job-header">
        <div className="rentee-job-info">
          <div className="job-filename">{displayName}</div>
          <div className="job-id">
            {job.machine_gpu || "Unknown GPU"} &middot; {id ? `${id.slice(0, 8)}...` : "unknown"}
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
          {downloadState?.summary ? <span>({downloadState.summary})</span> : null}
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
          {canViewFrames && (
            <button className="btn btn-secondary" onClick={onToggleGallery}>
              {galleryOpen ? "Hide Frames" : "View Frames"}
            </button>
          )}
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

function FrameGalleryPanel({ id, files, loading, error, openingFrameKey, onOpenFrame, backendUrl }) {
  return (
    <div className="job-frame-gallery">
      <div className="job-frame-gallery-head">
        <span>Live Frames</span>
        <span className="muted">{files.length} available</span>
      </div>

      {loading && files.length === 0 && (
        <div className="job-frame-gallery-empty">Loading frames...</div>
      )}

      {!loading && !error && files.length === 0 && (
        <div className="job-frame-gallery-empty">No frames available yet.</div>
      )}

      {error && (
        <div className="rentee-job-error job-frame-gallery-error">{error}</div>
      )}

      {files.length > 0 && (
        <div className="job-frame-grid">
          {files.map((file) => {
            const fileKey = `${id}:${file.job_id || ""}:${file.filename}`;
            const isOpening = openingFrameKey === fileKey;
            return (
              <button
                key={fileKey}
                className="job-frame-tile"
                type="button"
                disabled={isOpening}
                title={file.filename}
                onClick={() => onOpenFrame(file)}
              >
                <FrameThumb
                  className="job-frame-thumb"
                  file={file}
                  backendUrl={backendUrl}
                />
                <span className="job-frame-name">
                  {isOpening ? "Caching full frame..." : file.filename}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function FrameViewerModal({ viewer, onClose }) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const draggingRef = useRef(null);

  useEffect(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, [viewer?.imageSrc]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        onClose();
      }
      if (event.key === "0") {
        setScale(1);
        setOffset({ x: 0, y: 0 });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const applyZoom = (nextValue) => {
    setScale(Math.max(1, Math.min(8, nextValue)));
  };

  const onWheel = (event) => {
    event.preventDefault();
    applyZoom(scale + (event.deltaY > 0 ? -0.16 : 0.16));
  };

  const onMouseDown = (event) => {
    if (viewer?.loading || viewer?.error) return;
    draggingRef.current = { x: event.clientX, y: event.clientY };
  };

  const onMouseMove = (event) => {
    if (!draggingRef.current) return;
    const dx = event.clientX - draggingRef.current.x;
    const dy = event.clientY - draggingRef.current.y;
    draggingRef.current = { x: event.clientX, y: event.clientY };
    setOffset((prev) => ({ x: prev.x + dx, y: prev.y + dy }));
  };

  const clearDrag = () => {
    draggingRef.current = null;
  };

  return (
    <div className="frame-viewer-modal" onClick={onClose}>
      <div className="frame-viewer-card" onClick={(event) => event.stopPropagation()}>
        <div className="frame-viewer-head">
          <div>
            <strong>{viewer?.title || "Frame"}</strong>
            {viewer?.action ? (
              <span className="frame-viewer-subtext"> ({viewer.action})</span>
            ) : null}
          </div>
          <div className="frame-viewer-actions">
            <button className="btn btn-secondary" type="button" onClick={() => applyZoom(scale - 0.2)}>-</button>
            <button className="btn btn-secondary" type="button" onClick={() => applyZoom(scale + 0.2)}>+</button>
            <button
              className="btn btn-secondary"
              type="button"
              onClick={() => {
                setScale(1);
                setOffset({ x: 0, y: 0 });
              }}
            >
              Reset
            </button>
            <button className="btn btn-secondary" type="button" onClick={onClose}>Close</button>
          </div>
        </div>

        {viewer?.loading ? (
          <div className="frame-viewer-status">Downloading full-resolution frame...</div>
        ) : viewer?.error ? (
          <div className="frame-viewer-status frame-viewer-error">{viewer.error}</div>
        ) : (
          <div
            className="frame-viewer-canvas"
            onWheel={onWheel}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={clearDrag}
            onMouseLeave={clearDrag}
          >
            <img
              className="frame-viewer-image"
              src={viewer?.imageSrc || ""}
              alt={viewer?.title || "Rendered frame"}
              draggable={false}
              style={{
                transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
                cursor: scale > 1 ? "grab" : "default",
              }}
            />
          </div>
        )}

        {viewer?.localPath ? (
          <div className="frame-viewer-foot">
            Saved at <code>{viewer.localPath}</code>
          </div>
        ) : null}
      </div>
    </div>
  );
}
