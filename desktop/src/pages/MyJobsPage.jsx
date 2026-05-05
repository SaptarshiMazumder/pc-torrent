import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import {
  cancelRenderGroup,
  cancelAllRenderGroups,
  getFirebaseToken,
  getRenderGroup,
  getRenderGroupOutputs,
} from "../services/api";
import { cacheViewerFrame } from "../services/sidecar";
import {
  jobKey,
  resolveJobFilename,
  outputSort,
} from "../utils/jobUtils";
import {
  getTerminalDetailFromCache,
  setTerminalDetailInCache,
} from "../utils/terminalDetailCache";
import JobGrid from "../components/jobs/JobGrid";
import JobDetailView from "../components/jobs/JobDetailView";
import FrameViewerModal from "../components/jobs/FrameViewerModal";
import { useDownloads } from "../contexts/DownloadContext";

const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

export default function MyJobsPage({ jobs, loading, hasMore, loadingMore, onLoadMore, removeJob, backendUrl, markRenderGroupCancelled, onRefresh, onNavigate }) {
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [cancelingGroupIds, setCancelingGroupIds] = useState({});
  const [cancelingAll, setCancelingAll] = useState(false);
  const [openFrameGalleries, setOpenFrameGalleries] = useState({});
  const [frameGalleries, setFrameGalleries] = useState({});
  const [authToken, setAuthToken] = useState("");
  const [openingFrameKey, setOpeningFrameKey] = useState("");
  const [frameViewer, setFrameViewer] = useState(null);
  // Terminal groups come back slim from the list endpoint (no `tasks`, etc.)
  // — fetch the full DTO via /render-groups/{id} when one is selected,
  // and cache it in localStorage forever (terminal data never changes).
  const [terminalDetailJob, setTerminalDetailJob] = useState(null);
  const { downloads, startDownload } = useDownloads();
  const sentinelRef = useRef(null);

  // Auto-fetch the next page when the sentinel is in view.  Re-attach
  // on every ``jobs.length`` change so we re-check intersection after
  // each successful load -- IntersectionObserver only fires when the
  // intersection state *changes*, so a sentinel that was already visible
  // and stays visible (because the page is taller than the loaded slice)
  // would never re-fire without re-attachment.  ``rootMargin: 200px``
  // fires slightly before the user hits the literal bottom.
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !hasMore) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) onLoadMore();
    }, { rootMargin: "200px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, jobs.length, onLoadMore]);

  // If selected job gets removed, go back to grid
  useEffect(() => {
    if (selectedJobId && !jobs.find((j) => jobKey(j) === selectedJobId)) {
      setSelectedJobId(null);
    }
  }, [jobs, selectedJobId]);

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

  const handleCancelRenderGroup = async (groupId) => {
    if (!groupId || cancelingGroupIds[groupId]) return;
    setCancelingGroupIds((prev) => ({ ...prev, [groupId]: true }));
    try {
      await cancelRenderGroup(backendUrl, groupId);
      markRenderGroupCancelled?.(groupId);
    } catch {
      // cancel error is shown inline via cancelingGroupIds state
    } finally {
      setCancelingGroupIds((prev) => {
        const next = { ...prev };
        delete next[groupId];
        return next;
      });
    }
  };

  const handleCancelAll = async () => {
    if (cancelingAll) return;
    setCancelingAll(true);
    try {
      await cancelAllRenderGroups(backendUrl);
      onRefresh?.();
    } catch {
      // silently fail
    } finally {
      setCancelingAll(false);
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
        const payload = await getRenderGroupOutputs(backendUrl, id);
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

const selectedJob = selectedJobId ? jobs.find((j) => jobKey(j) === selectedJobId) : null;
  const selectedStatus = selectedJob?.status;
  const isTerminalSelection = !!selectedStatus && TERMINAL_STATUSES.has(selectedStatus);

  // Read cache synchronously during render — guarantees no flash of the
  // slim list DTO before the cache or fetch fills in.  useMemo recomputes
  // when the selection (or its terminal-ness) changes.
  const cachedTerminalJob = useMemo(() => {
    if (!selectedJobId || !isTerminalSelection) return null;
    return getTerminalDetailFromCache(selectedJobId);
  }, [selectedJobId, isTerminalSelection]);

  // Network-fetched fallback when cache misses.  Terminal data is immutable,
  // so we only fetch once per group_id ever (across sessions, thanks to
  // localStorage).  Active groups never enter this code path.
  useEffect(() => {
    if (!selectedJobId || !isTerminalSelection || cachedTerminalJob) {
      setTerminalDetailJob(null);
      return;
    }
    setTerminalDetailJob(null);
    let cancelled = false;
    getRenderGroup(backendUrl, selectedJobId)
      .then((data) => {
        if (cancelled) return;
        setTerminalDetailJob(data);
        setTerminalDetailInCache(selectedJobId, data);
      })
      .catch(() => {
        // Stays null — spinner stays visible.  User can click "Back to list".
      });
    return () => {
      cancelled = true;
    };
  }, [selectedJobId, isTerminalSelection, cachedTerminalJob, backendUrl]);

  // Use cached data first; otherwise the network result, but only if it
  // matches the currently-selected group_id (guards against showing the
  // previous group's data while a switch is in flight).
  const terminalData = useMemo(() => {
    if (!selectedJobId || !isTerminalSelection) return null;
    if (cachedTerminalJob) return cachedTerminalJob;
    if (terminalDetailJob && terminalDetailJob.group_id === selectedJobId) {
      return terminalDetailJob;
    }
    return null;
  }, [selectedJobId, isTerminalSelection, cachedTerminalJob, terminalDetailJob]);

  const jobForDetail = isTerminalSelection
    ? terminalData || selectedJob
    : selectedJob;
  // Spinner shows whenever a terminal group is selected and we haven't
  // got the rich DTO yet (neither from cache nor from the network).
  const showTerminalSpinner = !!selectedJob && isTerminalSelection && !terminalData;

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
      onDownload: () => {
        const dl = downloads[id];
        if (dl?.status === "loading") {
          onNavigate?.("downloads");
          return;
        }
        startDownload(
          id,
          displayName,
          () => getRenderGroupOutputs(backendUrl, id)
        );
        onNavigate?.("downloads");
      },
      onCancel: () => { void handleCancelRenderGroup(id); },
      onToggleGallery: () => handleToggleFrameGallery(job),
      onOpenFrame: (file) => { void handleOpenFrame(job, file); },
      onRemove: async () => {
        await removeJob(id);
        setSelectedJobId(null);
      },
      onRefreshFrames: () => { void fetchFrameGallery(job, { silent: false }); },
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
            className="btn btn-danger"
            type="button"
            onClick={handleCancelAll}
            disabled={cancelingAll || jobs.length === 0}
            style={{ marginLeft: "auto" }}
          >
            {cancelingAll ? "Cancelling..." : "Cancel All"}
          </button>
          <button
            className="btn btn-secondary"
            type="button"
            onClick={onRefresh}
            disabled={loading}
            style={{ marginLeft: 8 }}
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

      {!selectedJob && hasMore && (
        <div ref={sentinelRef} className="myjobs-load-more">
          {loadingMore ? "Loading more..." : ""}
        </div>
      )}

      {showTerminalSpinner && (
        <div className="detail-loading">
          <div className="detail-loading-spinner" aria-hidden="true">
            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
              <circle
                cx="16"
                cy="16"
                r="12"
                stroke="currentColor"
                strokeWidth="3"
                strokeDasharray="20 14"
                strokeLinecap="round"
              >
                <animateTransform
                  attributeName="transform"
                  type="rotate"
                  from="0 16 16"
                  to="360 16 16"
                  dur="0.9s"
                  repeatCount="indefinite"
                />
              </circle>
            </svg>
          </div>
          <p className="muted">Loading job details...</p>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={handleBack}
            style={{ marginTop: 12 }}
          >
            Back to list
          </button>
        </div>
      )}

      {jobForDetail && !showTerminalSpinner && (() => {
        const dlState = downloads[selectedJobId];
        const contextDownloadState = dlState ? {
          status: dlState.status === "loading" ? "loading" : dlState.status,
          path: dlState.path || "",
          error: dlState.error || "",
          progress: dlState.totalFiles > 0 ? `${dlState.completedFiles} / ${dlState.totalFiles}` : "",
          summary: [
            dlState.downloaded > 0 ? `${dlState.downloaded} downloaded` : "",
            dlState.skipped > 0 ? `${dlState.skipped} skipped` : "",
            dlState.failed > 0 ? `${dlState.failed} failed` : "",
          ].filter(Boolean).join(", "),
        } : undefined;
        return (
          <JobDetailView
            job={jobForDetail}
            backendUrl={backendUrl}
            authToken={authToken}
            downloadState={contextDownloadState}
            downloadingId={dlState?.status === "loading" ? selectedJobId : null}
            canceling={!!cancelingGroupIds[selectedJobId]}
            galleryOpen={!!openFrameGalleries[selectedJobId]}
            galleryState={frameGalleries[selectedJobId]}
            openingFrameKey={openingFrameKey}
            onBack={handleBack}
            onRefresh={onRefresh}
            {...getHandlers(jobForDetail)}
          />
        );
      })()}

      {frameViewer && (
        <FrameViewerModal viewer={frameViewer} onClose={() => setFrameViewer(null)} />
      )}
    </div>
  );
}
