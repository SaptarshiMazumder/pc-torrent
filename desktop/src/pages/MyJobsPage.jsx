import { useCallback, useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import {
  cancelRenderGroup,
  getFirebaseToken,
  getRenderGroupOutputs,
  getJobOutputs,
} from "../services/api";
import { downloadJobOutputToDownloads, cacheViewerFrame } from "../services/sidecar";
import {
  jobKey,
  resolveJobFilename,
  buildDownloadFolderName,
  outputSort,
  summarizeDownloadActions,
} from "../utils/jobUtils";
import JobGrid from "../components/jobs/JobGrid";
import JobDetailView from "../components/jobs/JobDetailView";
import FrameViewerModal from "../components/jobs/FrameViewerModal";

// Persists download results across page navigations (component unmounts/remounts)
const _downloadResultsStore = {};

export default function MyJobsPage({ jobs, loading, removeJob, backendUrl, markRenderGroupCancelled, onRefresh, onReRender }) {
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [downloadingId, setDownloadingId] = useState(null);
  const [downloadResults, _setDownloadResults] = useState(() => ({ ..._downloadResultsStore }));
  const [cancelingGroupIds, setCancelingGroupIds] = useState({});
  const [openFrameGalleries, setOpenFrameGalleries] = useState({});
  const [frameGalleries, setFrameGalleries] = useState({});
  const [authToken, setAuthToken] = useState("");
  const [openingFrameKey, setOpeningFrameKey] = useState("");
  const [frameViewer, setFrameViewer] = useState(null);
  const jobsRef = useRef(jobs);

  useEffect(() => { jobsRef.current = jobs; }, [jobs]);

  // If selected job gets removed, go back to grid
  useEffect(() => {
    if (selectedJobId && !jobs.find((j) => jobKey(j) === selectedJobId)) {
      setSelectedJobId(null);
    }
  }, [jobs, selectedJobId]);

  const setDownloadResults = useCallback((updater) => {
    _setDownloadResults((prev) => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      Object.keys(_downloadResultsStore).forEach((k) => delete _downloadResultsStore[k]);
      Object.assign(_downloadResultsStore, next);
      return next;
    });
  }, []);

  // Auth token — refresh every 10 minutes
  useEffect(() => {
    let cancelled = false;
    const refreshToken = async () => {
      try {
        const token = await getFirebaseToken();
        if (!cancelled) setAuthToken(token || "");
      } catch {
        if (!cancelled) setAuthToken("");
      }
    };
    void refreshToken();
    const timer = setInterval(() => { void refreshToken(); }, 10 * 60 * 1000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const handleDownload = async (id, fetchOutputs, jobFilename) => {
    setDownloadResults((prev) => ({
      ...prev,
      [id]: { status: "loading", path: "", error: "", progress: "", summary: "" },
    }));
    setDownloadingId(id);
    try {
      const jobFolder = buildDownloadFolderName(jobFilename, id);
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
            overwriteExisting: false,
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
      setDownloadResults((prev) => ({
        ...prev,
        [groupId]: { status: "error", path: "", error: error?.message || "Failed to cancel", progress: "" },
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
          [id]: { ...prev[id], loading: true, error: "", files: prev[id]?.files || [] },
        }));
      }
      try {
        const payload = job.group_id
          ? await getRenderGroupOutputs(backendUrl, id)
          : await getJobOutputs(backendUrl, id);
        const files = Array.isArray(payload?.files) ? payload.files.slice().sort(outputSort) : [];
        setFrameGalleries((prev) => ({
          ...prev,
          [id]: { loading: false, error: "", files, updatedAt: Date.now() },
        }));
      } catch (err) {
        setFrameGalleries((prev) => ({
          ...prev,
          [id]: { loading: false, error: err?.message || "Failed to load frames", files: prev[id]?.files || [], updatedAt: Date.now() },
        }));
      }
    },
    [backendUrl]
  );

  const handleOpenFrame = useCallback(async (job, file) => {
    const id = jobKey(job);
    if (!id || !file?.url || !file?.filename) return;
    const fileKey = `${id}:${file.job_id || ""}:${file.filename}`;
    const cacheKey = `${id}_${file.job_id || ""}_${file.filename}`;
    setOpeningFrameKey(fileKey);
    setFrameViewer({ title: file.filename, loading: true, error: "", imageSrc: "", localPath: "", action: "" });
    try {
      const localPath = await cacheViewerFrame(
        file.url,
        cacheKey,
        Number.isFinite(file.size_bytes) ? file.size_bytes : null,
      );
      setFrameViewer({ title: file.filename, loading: false, error: "", imageSrc: convertFileSrc(localPath), localPath, action: "cached" });
    } catch (error) {
      setFrameViewer({ title: file.filename, loading: false, error: error?.message || "Failed to load frame", imageSrc: "", localPath: "", action: "" });
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
        if (isOpen) delete next[id];
        return next;
      });
      if (!openFrameGalleries[id]) {
        void fetchFrameGallery(job, { silent: false });
      }
    },
    [fetchFrameGallery, openFrameGalleries]
  );

  // Poll open galleries for live updates
  useEffect(() => {
    const openIds = Object.keys(openFrameGalleries).filter((id) => openFrameGalleries[id]);
    if (openIds.length === 0) return;
    const tick = () => {
      const currentJobs = jobsRef.current || [];
      for (const id of openIds) {
        const job = currentJobs.find((candidate) => jobKey(candidate) === id);
        if (job) void fetchFrameGallery(job, { silent: true });
      }
    };
    tick();
    const timer = setInterval(tick, 2500);
    return () => clearInterval(timer);
  }, [openFrameGalleries, fetchFrameGallery]);

  const selectedJob = selectedJobId ? jobs.find((j) => jobKey(j) === selectedJobId) : null;

  const handleSelectJob = useCallback((id) => {
    setSelectedJobId(id);
  }, []);

  const handleBack = useCallback(() => {
    setSelectedJobId(null);
  }, []);

  // Build handlers for selected job
  const getHandlers = (job) => {
    const id = jobKey(job);
    const displayName = resolveJobFilename(job);
    return {
      onDownload: () =>
        handleDownload(
          id,
          () => job.group_id ? getRenderGroupOutputs(backendUrl, id) : getJobOutputs(backendUrl, id),
          displayName
        ),
      onCancel: () => { void handleCancelRenderGroup(id); },
      onToggleGallery: () => handleToggleFrameGallery(job),
      onOpenFrame: (file) => { void handleOpenFrame(job, file); },
      onRemove: async () => {
        await removeJob(id);
        setSelectedJobId(null);
      },
      onReRender: () => onReRender(job),
    };
  };

  return (
    <div className="page">
      {!selectedJob && (
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
      )}

      {!selectedJob && jobs.length === 0 && (
        <div className="empty-state">
          <p>No jobs submitted yet.</p>
          <p className="muted">Go to Create Render to start your first job.</p>
        </div>
      )}

      {!selectedJob && jobs.length > 0 && (
        <JobGrid
          jobs={jobs}
          authToken={authToken}
          backendUrl={backendUrl}
          onSelect={handleSelectJob}
          onRemove={removeJob}
        />
      )}

      {selectedJob && (
        <JobDetailView
          job={selectedJob}
          backendUrl={backendUrl}
          authToken={authToken}
          downloadState={downloadResults[selectedJobId]}
          downloadingId={downloadingId}
          canceling={!!cancelingGroupIds[selectedJobId]}
          galleryOpen={!!openFrameGalleries[selectedJobId]}
          galleryState={frameGalleries[selectedJobId]}
          openingFrameKey={openingFrameKey}
          onBack={handleBack}
          {...getHandlers(selectedJob)}
        />
      )}

      {frameViewer && (
        <FrameViewerModal viewer={frameViewer} onClose={() => setFrameViewer(null)} />
      )}
    </div>
  );
}
