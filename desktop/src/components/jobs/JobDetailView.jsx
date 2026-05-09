import { useEffect, useState } from "react";
import {
  STATUS_LABELS,
  isTerminalStatus,
  terminalFallbackPct,
  resolveJobFilename,
  getLatestTaskWithOutput,
} from "../../utils/jobUtils";
import JobThumbnail from "./JobThumbnail";
import loaderGif from "../../assets/animations/heartbeat-loader.gif";
import { useLiveCostTick, liveActualCost } from "../../hooks/useLiveCostTick";
import VastInstancePanel from "./VastInstancePanel";
import ModalInstancePanel from "../ModalInstancePanel";
import CommunityInstancePanel from "./CommunityInstancePanel";
import FrameGalleryPanel from "./FrameGalleryPanel";
import HeavinessPanel from "./HeavinessPanel";
import FailedChunksPanel from "./FailedChunksPanel";
import PendingChunksPanel from "./PendingChunksPanel";
import { usePendingQueue } from "../../hooks/usePendingQueue";

const ACTIVE_DRAWER_KEY = "pcrent:jd_active_drawer:v1";

const DRAWER_TITLES = {
  instances: "Instances",
  scene: "Scene",
  costs: "Costs",
};

function readActiveDrawer() {
  try {
    const v = localStorage.getItem(ACTIVE_DRAWER_KEY);
    return v === "instances" || v === "scene" || v === "costs" ? v : null;
  } catch {
    return null;
  }
}

function writeActiveDrawer(value) {
  try {
    if (value) localStorage.setItem(ACTIVE_DRAWER_KEY, value);
    else localStorage.removeItem(ACTIVE_DRAWER_KEY);
  } catch {
    // storage unavailable — drawer state simply won't persist
  }
}

function BigGauge({ pct, color, label }) {
  const size = 96;
  const stroke = 7;
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - ((pct ?? 0) / 100) * circ;
  return (
    <div className="jd-gauge">
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(180,175,220,0.08)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color}
          strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={circ} strokeDashoffset={offset}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: "stroke-dashoffset 0.5s ease", filter: `drop-shadow(0 0 6px ${color}44)` }}
        />
        <text x="50%" y="46%" textAnchor="middle" dominantBaseline="central"
          fill="#eeedf5" fontSize="22" fontWeight="800" fontFamily="Inter,sans-serif">
          {pct != null ? `${pct}%` : "—"}
        </text>
        <text x="50%" y="66%" textAnchor="middle" dominantBaseline="central"
          fill="#6e6893" fontSize="10" fontWeight="600">
          {label || "PERCENT"}
        </text>
      </svg>
    </div>
  );
}

function StatTile({ icon, label, value }) {
  return (
    <div className="jd-stat-tile">
      <div className="jd-stat-icon">{icon}</div>
      <div className="jd-stat-text">
        <span className="jd-stat-value">{value}</span>
        <span className="jd-stat-label">{label}</span>
      </div>
    </div>
  );
}

function RenderGroupDetail({
  job, backendUrl, authToken, tasksLoading, downloadState, downloadingId, canceling,
  galleryOpen, galleryState, openingFrameKey,
  onDownload, onCancel, onToggleGallery, onOpenFrame, onRemove,
  onRefresh, onRefreshFrames,
}) {
  const id = job.group_id;
  const rawPct = typeof job.overall_progress_pct === "number" ? Math.max(0, Math.min(100, job.overall_progress_pct)) : null;
  const overallPct = rawPct !== null ? Math.round(rawPct) : (job.status === "done" ? 100 : job.status === "cancelled" || job.status === "failed" ? 0 : null);
  const tasksDone = (job.tasks || []).filter((t) => t.status === "done").length;
  const taskCount = (job.tasks || []).length;
  const rendered = job.overall_rendered_frames || 0;
  const total = typeof job.total_frames === "number" ? job.total_frames : null;
  const availableOutputCount = typeof job.available_output_files_count === "number"
    ? job.available_output_files_count
    : (job.tasks || []).reduce((sum, t) => sum + (t.output_files_count || 0), 0);
  const canDownloadAvailable = availableOutputCount > 0;
  const isDownloading = downloadingId === id;
  const canCancel = ["pending", "running", "uploading"].includes(job.status);

  const gaugeColor = job.status === "done" ? "#22c55e" : job.status === "failed" ? "#ef4444" : job.status === "cancelled" ? "#6b7280" : "#e8724a";
  const canViewFrames = availableOutputCount > 0;

  const [activeDrawer, setActiveDrawer] = useState(readActiveDrawer);
  const instancesOpen = activeDrawer === "instances";
  const sceneOpen = activeDrawer === "scene";
  const costsOpen = activeDrawer === "costs";
  useEffect(() => { writeActiveDrawer(activeDrawer); }, [activeDrawer]);
  const toggleDrawer = (which) => setActiveDrawer((cur) => (cur === which ? null : which));

  const tasksList = job.tasks || [];

  // Pending-queue hook lives at this level so both the failed-chunks
  // panel (which fires a one-shot refresh on retry success) and the
  // pending panel (which renders the items + manual refresh) share
  // the same state.  10s gated polling continues as before.
  const pendingQueue = usePendingQueue(backendUrl, id, {
    tasks: tasksList,
    totalFrames: job.total_frames,
    groupStatus: job.status,
  });

  const latestOutputFile =
    job.latest_output_file || getLatestTaskWithOutput(job.tasks)?.latest_output_file || null;

  // Sum of per-task projected costs.  Tasks pre-AllocationPlanner have null
  // estimates -- treated as 0 so we don't poison the rollup.  When everything
  // is null/0 we hide the row entirely (no point showing "$0.00 est.").
  const totalEstimatedCost = tasksList.reduce(
    (sum, t) => sum + (typeof t.estimated_cost_usd === "number" ? t.estimated_cost_usd : 0),
    0,
  );

  // 1Hz tick so the actual-cost rollup recomputes for in-flight tasks
  // between polls.  liveActualCost mirrors the server's serializer formula
  // exactly so the live value lines up with the canonical one the next
  // poll will deliver.
  useLiveCostTick();
  const now = new Date();
  const totalActualCost = tasksList.reduce((sum, t) => {
    if (t.status === "running" || t.status === "uploading") {
      const live = liveActualCost(t, now);
      return sum + (live ?? 0);
    }
    return sum + (typeof t.actual_cost_usd === "number" ? t.actual_cost_usd : 0);
  }, 0);
  // Server already provides ``total_actual_cost_usd`` on the group payload
  // for terminal groups (where ``now`` doesn't matter).  Prefer the live
  // sum so running tasks tick smoothly; fall back to the server value
  // when the live sum is 0 but the server snapshot has data (covers
  // pre-tick first render).
  const groupActualCost = totalActualCost > 0
    ? totalActualCost
    : (typeof job.total_actual_cost_usd === "number" ? job.total_actual_cost_usd : 0);

  return (
    <div className="jd-body jd-body-split">
      <div className="jd-detail-grid">
        <div className="jd-left-col">
          <div className="jd-headline">
            <div className="jd-headline-main">
              <div className="jd-headline-value" style={{ color: gaugeColor }}>
                {overallPct != null ? `${overallPct}%` : "—"}
              </div>
              <div className="jd-headline-label">COMPLETE</div>
            </div>
            <div className="jd-headline-progress">
              <div className="jd-headline-frames">
                <span className="jd-headline-frames-label">Frames</span>
                <span className="jd-headline-frames-value">
                  {total != null ? `${rendered} / ${total}` : `${rendered}`}
                </span>
              </div>
              <div className="jd-headline-bar">
                <div
                  className="jd-headline-bar-fill"
                  style={{ width: `${overallPct ?? 0}%`, background: gaugeColor }}
                />
              </div>
              {totalEstimatedCost > 0 && (
                <div className="jd-headline-cost">
                  <span className="jd-headline-cost-label">Estimated cost</span>
                  <span className="jd-headline-cost-value">~${totalEstimatedCost.toFixed(2)}</span>
                </div>
              )}
            </div>
          </div>

          <div className="jd-stat-grid">
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>}
                label="Frames" value={total != null ? `${rendered} / ${total}` : `${rendered}`}
              />
            </div>
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 14h.01M10 14h.01" /></svg>}
                label="Machines" value={tasksLoading ? "…" : `${tasksDone} / ${taskCount}`}
              />
            </div>
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>}
                label="Output Files" value={availableOutputCount}
              />
            </div>
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" /></svg>}
                label="Failed" value={tasksLoading ? "…" : (tasksList.filter((t) => t.status === "failed")).length}
              />
            </div>
          </div>

          {latestOutputFile ? (
            <div className="jd-preview-card">
              <div className="jd-preview-thumb">
                <JobThumbnail job={job} authToken={authToken} backendUrl={backendUrl} />
              </div>
              <div className="jd-preview-meta">
                <span className="jd-preview-name">{latestOutputFile}</span>
                <span className="jd-preview-tag">Latest Rendered Frame</span>
              </div>
            </div>
          ) : (
            <img src={loaderGif} alt="Rendering..." className="jd-preview-loader-bare" />
          )}

          <div className={`jd-actions${latestOutputFile ? "" : " jd-actions--centered"}`}>
            {canCancel && (
              <button className="btn btn-danger" onClick={onCancel} disabled={canceling}>
                {canceling ? "Stopping..." : "Stop Render"}
              </button>
            )}
            {canDownloadAvailable && (
              <button className="btn btn-primary" onClick={onDownload} disabled={isDownloading}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>
                {isDownloading ? (downloadState?.progress || "Downloading...") : job.status === "done" ? "Download All" : "Download Available"}
              </button>
            )}
            <button className="btn btn-secondary" onClick={onRemove}>Remove</button>
          </div>
        </div>

        <div className="jd-right-col">
          {job.status === "failed" && job.error && <div className="inst-error">{job.error}</div>}
          {downloadState?.status === "done" && (
            <div className="rentee-job-success">Downloaded to <code>{downloadState.path}</code>{downloadState?.summary ? ` (${downloadState.summary})` : ""}</div>
          )}
          {downloadState?.status === "error" && <div className="inst-error">{downloadState.error}</div>}

          {tasksLoading ? (
            <div className="jd-section-loader">Loading instances and chunks…</div>
          ) : (
            <>
              <VastInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="active" />
              <ModalInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="active" />
              <CommunityInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="active" />

              <FailedChunksPanel
                tasks={tasksList}
                backendUrl={backendUrl}
                groupId={id}
                onRefresh={onRefresh}
                onPendingQueueRefresh={pendingQueue.refresh}
              />
            </>
          )}
          <PendingChunksPanel pendingQueue={pendingQueue} />

          {canViewFrames && (
            <div className="jd-frames-section">
              <div className="jd-frames-header">
                <button type="button" className="jd-frames-toggle" onClick={onToggleGallery}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>
                  <span>Rendered Frames</span>
                  <span className="jd-frames-count">
                    {availableOutputCount}
                    {galleryOpen && (galleryState?.files?.length ?? 0) > 0 && ` (${galleryState.files.length})`}
                  </span>
                  <svg className={`jd-frames-chevron${galleryOpen ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M6 9l6 6 6-6" /></svg>
                </button>
                {galleryOpen && onRefreshFrames && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={onRefreshFrames}
                    disabled={!!galleryState?.loading}
                    style={{ padding: "4px 10px", fontSize: 12 }}
                  >
                    {galleryState?.loading ? "Refreshing..." : "Refresh"}
                  </button>
                )}
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
          )}
        </div>
      </div>

      {activeDrawer && (
        <aside className="jd-drawer-panel">
          <div className="jd-drawer-panel-header">
            <span className="jd-drawer-panel-title">{DRAWER_TITLES[activeDrawer]}</span>
            <button
              type="button"
              className="jd-drawer-panel-close"
              onClick={() => setActiveDrawer(null)}
              title={`Hide ${DRAWER_TITLES[activeDrawer].toLowerCase()}`}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="jd-drawer-panel-body">
            {instancesOpen && (
              tasksLoading ? (
                <div className="jd-section-loader">Loading instances…</div>
              ) : (
                <>
                  <VastInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="finished" />
                  <ModalInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="finished" />
                  <CommunityInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="finished" />
                </>
              )
            )}
            {sceneOpen && (
              <HeavinessPanel
                heaviness={job.heaviness ?? null}
                overrides={job.resolved_render_settings?.render ?? null}
                loading={!job.heaviness}
              />
            )}
            {costsOpen && (
              <>
                <div className="jd-cost-summary">
                  <div className="jd-cost-summary-row">
                    <span className="jd-cost-summary-label">Estimated</span>
                    <span className="jd-cost-summary-value">
                      {tasksLoading ? "…" : totalEstimatedCost > 0 ? `~$${totalEstimatedCost.toFixed(2)}` : "—"}
                    </span>
                  </div>
                  <div className="jd-cost-summary-row">
                    <span className="jd-cost-summary-label">Actual</span>
                    <span className="jd-cost-summary-value">
                      {tasksLoading ? "…" : groupActualCost > 0 ? `$${groupActualCost.toFixed(2)}` : "—"}
                    </span>
                  </div>
                </div>

                {tasksLoading ? (
                  <div className="jd-cost-empty">Loading per-chunk costs…</div>
                ) : tasksList.length > 0 ? (
                  <ul className="jd-cost-list">
                    {tasksList
                      .slice()
                      .sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0))
                      .map((t) => {
                        const range = t.frame_start != null && t.frame_end != null
                          ? `${t.frame_start}–${t.frame_end}`
                          : "—";
                        const est = typeof t.estimated_cost_usd === "number"
                          ? `~$${t.estimated_cost_usd.toFixed(2)}`
                          : "—";
                        const liveCost = (t.status === "running" || t.status === "uploading")
                          ? liveActualCost(t, now)
                          : (typeof t.actual_cost_usd === "number" ? t.actual_cost_usd : null);
                        const actual = liveCost != null ? `$${liveCost.toFixed(2)}` : "—";
                        return (
                          <li key={t.job_id} className="jd-cost-item">
                            <span className="jd-cost-item-range">{range}</span>
                            <span className="jd-cost-item-machine" title={t.machine_gpu}>
                              {t.machine_gpu || "Unassigned"}
                            </span>
                            <span className="jd-cost-item-value jd-cost-item-est">{est}</span>
                            <span className="jd-cost-item-value">{actual}</span>
                          </li>
                        );
                      })}
                  </ul>
                ) : (
                  <div className="jd-cost-empty">
                    No estimates yet — costs will appear once chunks are dispatched.
                  </div>
                )}
              </>
            )}
          </div>
        </aside>
      )}

      <nav className={`jd-dock${activeDrawer ? " jd-dock-shifted" : ""}`} aria-label="Drawers">
        <button
          type="button"
          className={`jd-dock-btn${instancesOpen ? " jd-dock-btn-active" : ""}`}
          onClick={() => toggleDrawer("instances")}
          title={instancesOpen ? "Hide instances" : "Show instances"}
          aria-pressed={instancesOpen}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="4" y="4" width="16" height="16" rx="2" />
            <rect x="9" y="9" width="6" height="6" />
            <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" />
          </svg>
        </button>
        <button
          type="button"
          className={`jd-dock-btn${sceneOpen ? " jd-dock-btn-active" : ""}`}
          onClick={() => toggleDrawer("scene")}
          title={sceneOpen ? "Hide scene details" : "Show scene details"}
          aria-pressed={sceneOpen}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
            <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
            <line x1="12" y1="22.08" x2="12" y2="12" />
          </svg>
        </button>
        <button
          type="button"
          className={`jd-dock-btn${costsOpen ? " jd-dock-btn-active" : ""}`}
          onClick={() => toggleDrawer("costs")}
          title={costsOpen ? "Hide costs" : "Show costs"}
          aria-pressed={costsOpen}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M16 8h-6.5a2.5 2.5 0 0 0 0 5h3a2.5 2.5 0 0 1 0 5H6" />
            <path d="M12 6v2M12 16v2" />
          </svg>
        </button>
      </nav>
    </div>
  );
}

function SingleJobDetail({
  job, backendUrl, authToken, downloadState, downloadingId,
  galleryOpen, galleryState, openingFrameKey,
  onDownload, onToggleGallery, onOpenFrame, onRemove,
  onRefreshFrames,
}) {
  const id = job.job_id;
  const totalFrames = typeof job.total_frames === "number" ? job.total_frames : null;
  const renderedFrames = typeof job.rendered_frames === "number" ? job.rendered_frames : 0;
  const rawPct = typeof job.progress_pct === "number" ? Math.max(0, Math.min(100, job.progress_pct)) : null;
  const progressPct = rawPct !== null ? Math.round(rawPct) : (job.status === "done" ? 100 : job.status === "cancelled" || job.status === "failed" ? 0 : null);
  const hasTotalFrames = typeof totalFrames === "number" && totalFrames > 0;
  const availableOutputCount = typeof job.output_files_count === "number" ? job.output_files_count : (Array.isArray(job.output_files) ? job.output_files.length : 0);
  const canDownloadAvailable = availableOutputCount > 0;
  const gaugeColor = job.status === "done" ? "#22c55e" : job.status === "failed" ? "#ef4444" : "#e8724a";

  return (
    <div className="jd-body">
      <div className="jd-top-grid">
        <div className="jd-card jd-card-gauge">
          <BigGauge pct={progressPct} color={gaugeColor} label="COMPLETE" />
          <div className="jd-gauge-stats">
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>}
              label="Frames" value={hasTotalFrames ? `${renderedFrames} / ${totalFrames}` : `${renderedFrames}`}
            />
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>}
              label="Output Files" value={availableOutputCount}
            />
          </div>
        </div>
      </div>

      {job.status === "failed" && job.error && <div className="inst-error">{job.error}</div>}
      {downloadState?.status === "done" && (
        <div className="rentee-job-success">Downloaded to <code>{downloadState.path}</code>{downloadState?.summary ? ` (${downloadState.summary})` : ""}</div>
      )}
      {downloadState?.status === "error" && <div className="inst-error">{downloadState.error}</div>}

      {/* Frames — collapsible */}
      {canDownloadAvailable && (
        <div className="jd-frames-section">
          <div className="jd-frames-header">
            <button type="button" className="jd-frames-toggle" onClick={onToggleGallery}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>
              <span>Rendered Frames</span>
              <span className="jd-frames-count">
                {availableOutputCount}
                {galleryOpen && (galleryState?.files?.length ?? 0) > 0 && ` (${galleryState.files.length})`}
              </span>
              <svg className={`jd-frames-chevron${galleryOpen ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M6 9l6 6 6-6" /></svg>
            </button>
            {galleryOpen && onRefreshFrames && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={onRefreshFrames}
                disabled={!!galleryState?.loading}
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                {galleryState?.loading ? "Refreshing..." : "Refresh"}
              </button>
            )}
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
      )}

      <div className="jd-actions">
        {canDownloadAvailable && (
          <button className="btn btn-primary" onClick={onDownload} disabled={downloadingId === id}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>
            {downloadingId === id ? "Downloading..." : job.status === "done" ? "Download" : "Download Available"}
          </button>
        )}
        <button className="btn btn-secondary" onClick={onRemove}>Remove</button>
      </div>
    </div>
  );
}

export default function JobDetailView({
  job, backendUrl, authToken, tasksLoading, downloadState, downloadingId, canceling,
  galleryOpen, galleryState, openingFrameKey, refreshing,
  onBack, onDownload, onCancel, onToggleGallery, onOpenFrame, onRemove,
  onRefresh, onRefreshFrames,
}) {
  const isGroup = !!job?.group_id;
  const displayName = resolveJobFilename(job);
  const status = job?.status || "pending";
  const id = job?.group_id || job?.job_id || "";
  // Prefer the slim list endpoint's tasks_count snapshot while the full
  // tasks payload is still in flight, so the header chip doesn't flash
  // "0 machines" before settling on the real value.
  const taskCount = isGroup
    ? (job.tasks?.length ?? job.tasks_count ?? 0)
    : 1;

  return (
    <div className="job-detail">
      <div className="job-detail-header">
        <div className="jd-header-top" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button type="button" className="job-detail-back" onClick={onBack}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M19 12H5M12 5l-7 7 7 7" /></svg>
            All Jobs
          </button>
          <button
            type="button"
            className="job-detail-back"
            onClick={onRefresh}
            disabled={!!refreshing}
            style={{ opacity: refreshing ? 0.5 : 1 }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></svg>
            {refreshing ? "Refreshing..." : "Refresh"}
          </button>
        </div>

        <div className="job-detail-hero">
          <div className="job-detail-hero-icon">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z" />
              <circle cx="12" cy="13" r="3" />
            </svg>
          </div>
          <div className="job-detail-title">
            <h2>{displayName}</h2>
            <div className="job-detail-meta-row">
              <span className={`status-badge status-${status}`}>
                {status === "running" && <span className="status-badge-dot" />}
                {STATUS_LABELS[status] || status}
              </span>
              <span className="job-detail-meta-chip">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 14h.01M10 14h.01" /></svg>
                {taskCount} machine{taskCount !== 1 ? "s" : ""}
              </span>
              <span className="job-detail-id">{id ? id.slice(0, 12) + "..." : ""}</span>
              {job?.submitted_at && (
                <span className="job-detail-date">
                  {new Date(job.submitted_at).toLocaleDateString()} {new Date(job.submitted_at).toLocaleTimeString()}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {isGroup ? (
        <RenderGroupDetail
          job={job} backendUrl={backendUrl} authToken={authToken} tasksLoading={tasksLoading}
          downloadState={downloadState} downloadingId={downloadingId} canceling={canceling}
          galleryOpen={galleryOpen} galleryState={galleryState} openingFrameKey={openingFrameKey}
          onDownload={onDownload} onCancel={onCancel} onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame} onRemove={onRemove}
          onRefresh={onRefresh} onRefreshFrames={onRefreshFrames}
        />
      ) : (
        <SingleJobDetail
          job={job} backendUrl={backendUrl} authToken={authToken}
          downloadState={downloadState} downloadingId={downloadingId}
          galleryOpen={galleryOpen} galleryState={galleryState} openingFrameKey={openingFrameKey}
          onDownload={onDownload} onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame} onRemove={onRemove}
          onRefreshFrames={onRefreshFrames}
        />
      )}
    </div>
  );
}
