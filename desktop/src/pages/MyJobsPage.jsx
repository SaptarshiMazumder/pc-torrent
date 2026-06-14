import { useCallback, useEffect, useMemo, useState } from "react";
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
  clearTerminalDetailFromCache,
} from "../utils/terminalDetailCache";
import JobGrid from "../components/jobs/JobGrid";
import JobDetailView from "../components/jobs/JobDetailView";
import FrameViewerModal from "../components/jobs/FrameViewerModal";
import Loader from "../components/common/Loader";
import { useDownloads } from "../contexts/DownloadContext";
import { useError } from "../contexts/ErrorContext";

const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

export default function MyJobsPage({
  ongoingJobs,
  pastJobs,
  loadingOngoing,
  loadingPast,
  loadingMoreOngoing,
  loadingMorePast,
  hasMoreOngoing,
  hasMorePast,
  loadMoreOngoing,
  loadMorePast,
  removeJob,
  backendUrl,
  markRenderGroupCancelled,
  updateGroup,
  onRefresh,
  onNavigate,
  initialSelectedJobId = null,
}) {
  // Combined view for cross-section lookups (selection, gallery fetch).
  // The two list slices stay separate for rendering + pagination.
  const allJobs = useMemo(
    () => [...ongoingJobs, ...pastJobs],
    [ongoingJobs, pastJobs],
  );
  // Seeded from a deep-link (e.g. just-submitted render) so we open straight
  // into that job's detail view.  Mount-only: this page remounts on every
  // navigation to it, so the prop is re-read each fresh visit.
  const [selectedJobId, setSelectedJobId] = useState(initialSelectedJobId);
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
  const { showError } = useError();

  // If selected job gets removed, go back to grid
  useEffect(() => {
    if (selectedJobId && !allJobs.find((j) => jobKey(j) === selectedJobId)) {
      setSelectedJobId(null);
    }
  }, [allJobs, selectedJobId]);

  // Auto-refresh on page visit when nothing's in the cache.  ``useJobs``
  // lives at App level so it persists across navigation -- the initial
  // fetch fires once on app mount, but a fresh visit to /myjobs after
  // that gets stale state (or empty state if the initial fetch failed).
  // Trigger a refresh on mount, but only when both lists are empty AND
  // nothing is already loading -- avoids stomping a fetch in flight or
  // re-pulling data the user already has.
  useEffect(() => {
    if (!ongoingJobs.length && !pastJobs.length && !loadingOngoing && !loadingPast) {
      onRefresh?.();
    }
    // Mount-only: refresh decision is based on the initial render's
    // snapshot.  Polling keeps things fresh after that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

const selectedJob = selectedJobId ? allJobs.find((j) => jobKey(j) === selectedJobId) : null;
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
        // Network error -- terminal data stays unfilled.  The detail view
        // already renders off the slim list DTO so the user just doesn't
        // get the rich panels until they hit Refresh.
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
  // True only while the slow /render-groups/{id} fetch is in flight for
  // an uncached terminal group.  Active groups already carry tasks via
  // the list endpoint; cache hits land synchronously.  The detail view
  // uses this to render the page immediately off the slim list DTO and
  // show inline loaders on the panels that need the full tasks array.
  const tasksLoading = !!selectedJob && isTerminalSelection && !terminalData;

  const handleSelectJob = useCallback((id) => {
    setSelectedJobId(id);
    // Default the frames gallery to open on each navigation.  The
    // useEffect below kicks the actual fetch when needed.
    setOpenFrameGalleries((prev) => (prev[id] ? prev : { ...prev, [id]: true }));
  }, []);

  // First-time fetch for the frames gallery when a job is selected and
  // its gallery is open (which it is by default after handleSelectJob).
  // Guarded on the *presence* of an entry, not on its file count, so
  // that the in-flight ``loading: true, files: []`` snapshot doesn't
  // re-trigger this effect each time setFrameGalleries fires from
  // inside fetchFrameGallery -- otherwise polling-driven allJobs
  // updates would stampede the /outputs endpoint.  The Refresh button
  // calls fetchFrameGallery directly when the user wants a re-fetch.
  useEffect(() => {
    if (!selectedJobId) return;
    if (!openFrameGalleries[selectedJobId]) return;
    if (frameGalleries[selectedJobId]) return;
    const job = allJobs.find((j) => jobKey(j) === selectedJobId);
    if (!job) return;
    void fetchFrameGallery(job, { silent: false });
  }, [selectedJobId, openFrameGalleries, frameGalleries, allJobs, fetchFrameGallery]);

  const handleBack = useCallback(() => {
    setSelectedJobId(null);
  }, []);

  // Detail-page Refresh: fetch ONLY the selected group via
  // /render-groups/{id}.  No paginated list re-fetch — that would be
  // wasted work since the user only cares about the open detail.
  // Terminal groups also get their persistent localStorage cache busted
  // so a group flipping terminal -> pending -> terminal (e.g. via
  // manual retry) doesn't keep serving the original frozen DTO.
  const [refreshingDetail, setRefreshingDetail] = useState(false);
  const handleRefreshFromDetail = useCallback(async () => {
    if (!selectedJobId || !backendUrl) return;
    setRefreshingDetail(true);
    clearTerminalDetailFromCache(selectedJobId);
    setTerminalDetailJob(null);
    try {
      const data = await getRenderGroup(backendUrl, selectedJobId);
      if (!data) return;
      if (TERMINAL_STATUSES.has(data.status)) {
        setTerminalDetailInCache(selectedJobId, data);
        setTerminalDetailJob(data);
      } else {
        updateGroup?.(selectedJobId, data);
      }
    } catch {
      // network error — leave UI as-is, user can retry
    } finally {
      setRefreshingDetail(false);
    }
  }, [selectedJobId, backendUrl, updateGroup]);

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
        try {
          await removeJob(id);
          setSelectedJobId(null);
        } catch (err) {
          showError({
            title: "Couldn't delete render",
            message: err?.message || "Server rejected the delete request.",
            detail: err?.body || null,
          });
        }
      },
      onRefreshFrames: () => { void fetchFrameGallery(job, { silent: false }); },
    };
  };

  return (
    <div className="page">
      {!selectedJob && (
        <div className="page-header">
          <h2>My Jobs</h2>
          <button
            className="btn btn-danger"
            type="button"
            onClick={handleCancelAll}
            disabled={cancelingAll || ongoingJobs.length === 0}
            style={{ marginLeft: "auto" }}
          >
            {cancelingAll ? "Cancelling..." : "Cancel All"}
          </button>
          <button
            className="btn btn-secondary"
            type="button"
            onClick={onRefresh}
            disabled={loadingOngoing || loadingPast}
            style={{ marginLeft: 8 }}
          >
            {(loadingOngoing || loadingPast) ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      )}

      {!selectedJob && (loadingOngoing || ongoingJobs.length > 0) && (
        <section className="myjobs-section">
          <div className="myjobs-section-head">
            <h3>Ongoing renders</h3>
            <span className="log-count">
              {ongoingJobs.length}{hasMoreOngoing ? "+" : ""}
            </span>
          </div>
          {loadingOngoing && ongoingJobs.length === 0 ? (
            <div className="myjobs-section-loader">
              <Loader size="sm" />
            </div>
          ) : (
            <JobGrid
              jobs={ongoingJobs}
              authToken={authToken}
              backendUrl={backendUrl}
              onSelect={handleSelectJob}
              onRemove={removeJob}
              hasMore={hasMoreOngoing}
              loadingMore={loadingMoreOngoing}
              onLoadMore={loadMoreOngoing}
            />
          )}
          {loadingMoreOngoing && (
            <div className="myjobs-section-loader">
              <Loader size="sm" />
            </div>
          )}
        </section>
      )}

      {!selectedJob && (loadingPast || pastJobs.length > 0) && (
        <section className="myjobs-section">
          <div className="myjobs-section-head">
            <h3>Past renders</h3>
            <span className="log-count">
              {pastJobs.length}{hasMorePast ? "+" : ""}
            </span>
          </div>
          {loadingPast && pastJobs.length === 0 ? (
            <div className="myjobs-section-loader">
              <Loader size="sm" />
            </div>
          ) : (
            <JobGrid
              jobs={pastJobs}
              authToken={authToken}
              backendUrl={backendUrl}
              onSelect={handleSelectJob}
              onRemove={removeJob}
              hasMore={hasMorePast}
              loadingMore={loadingMorePast}
              onLoadMore={loadMorePast}
            />
          )}
          {loadingMorePast && (
            <div className="myjobs-section-loader">
              <Loader size="sm" />
            </div>
          )}
        </section>
      )}

      {jobForDetail && (() => {
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
            tasksLoading={tasksLoading}
            downloadState={contextDownloadState}
            downloadingId={dlState?.status === "loading" ? selectedJobId : null}
            canceling={!!cancelingGroupIds[selectedJobId]}
            galleryOpen={!!openFrameGalleries[selectedJobId]}
            galleryState={frameGalleries[selectedJobId]}
            openingFrameKey={openingFrameKey}
            refreshing={refreshingDetail}
            onBack={handleBack}
            onRefresh={handleRefreshFromDetail}
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
