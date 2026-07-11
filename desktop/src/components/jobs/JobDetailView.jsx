import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  jobStatusLabel,
  isTerminalStatus,
  terminalFallbackPct,
  resolveJobFilename,
  getLatestTaskWithOutput,
  countUniqueFrames,
} from "../../utils/jobUtils";
import JobThumbnail from "./JobThumbnail";
import VastInstancePanel from "./VastInstancePanel";
import ModalInstancePanel from "../ModalInstancePanel";
import CommunityInstancePanel from "./CommunityInstancePanel";
import FrameGalleryPanel from "./FrameGalleryPanel";
import HeavinessPanel from "./HeavinessPanel";
import FailedChunksPanel from "./FailedChunksPanel";
import PendingChunksPanel from "./PendingChunksPanel";
import Loader from "../common/Loader";
import { usePendingQueue } from "../../hooks/usePendingQueue";
import { formatCredits } from "../../utils/creditsFormat";

// Maps each drawer to the dock's "hide" tooltip key so the drawer's own
// close button reuses the same phrasing the dock uses.
const DRAWER_HIDE_KEYS = {
  instances: "detail.dock.hideInstances",
  scene: "detail.dock.hideScene",
  costs: "detail.dock.hideCosts",
};

function BigGauge({ pct, color, label }) {
  const size = 96;
  const stroke = 7;
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - ((pct ?? 0) / 100) * circ;
  return (
    <div className="jd-gauge">
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--hair)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color}
          strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={circ} strokeDashoffset={offset}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: "stroke-dashoffset 0.5s ease", filter: `drop-shadow(0 0 6px ${color}44)` }}
        />
        <text x="50%" y="46%" textAnchor="middle" dominantBaseline="central"
          fill="var(--text)" fontSize="22" fontWeight="800" fontFamily="var(--font-ui)">
          {pct != null ? `${pct}%` : "—"}
        </text>
        <text x="50%" y="66%" textAnchor="middle" dominantBaseline="central"
          fill="var(--muted)" fontSize="10" fontWeight="600">
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

// 108px progress ring for the render-group gauge card (ember arc over a hair
// track, rounded cap + glow). Purely presentational — pct/color are passed in.
function GroupGauge({ pct, color, label }) {
  const size = 108;
  const sw = 9;
  const r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const val = pct ?? 0;
  return (
    <div className="jd-ring" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--hair)" strokeWidth={sw} />
        <circle
          cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={sw} strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c - (val / 100) * c}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ filter: `drop-shadow(0 0 6px ${color}55)`, transition: "stroke-dashoffset 0.5s ease" }}
        />
      </svg>
      <div className="jd-ring-center">
        <b style={{ color }}>{pct != null ? `${pct}%` : "—"}</b>
        <span>{label}</span>
      </div>
    </div>
  );
}

function RenderGroupDetail({
  job, backendUrl, authToken, tasksLoading, downloadState, downloadingId, canceling,
  galleryOpen, galleryState, openingFrameKey,
  onDownload, onCancel, onToggleGallery, onOpenFrame, onOpenLatestFrame, onRemove,
  onRefresh, onRefreshFrames,
}) {
  const { t } = useTranslation(["myJobs", "common"]);
  const id = job.group_id;
  const rawPct = typeof job.overall_progress_pct === "number" ? Math.max(0, Math.min(100, job.overall_progress_pct)) : null;
  const overallPct = rawPct !== null ? Math.round(rawPct) : (job.status === "done" ? 100 : job.status === "cancelled" || job.status === "failed" ? 0 : null);
  const tasksDone = (job.tasks || []).filter((task) => task.status === "done").length;
  const taskCount = (job.tasks || []).length;
  const rendered = job.overall_rendered_frames || 0;
  const total = typeof job.total_frames === "number" ? job.total_frames : null;
  const availableOutputCount = typeof job.available_output_files_count === "number"
    ? job.available_output_files_count
    : (job.tasks || []).reduce((sum, task) => sum + (task.output_files_count || 0), 0);
  const canDownloadAvailable = availableOutputCount > 0;
  const isDownloading = downloadingId === id;
  const canCancel = ["pending", "running", "uploading"].includes(job.status);

  const gaugeColor = job.status === "done" ? "#12a150" : job.status === "failed" ? "#ef4444" : job.status === "cancelled" ? "#9a8d7b" : "#ee5a29";
  const canViewFrames = availableOutputCount > 0;

  const [activeDrawer, setActiveDrawer] = useState(null);
  const instancesOpen = activeDrawer === "instances";
  const sceneOpen = activeDrawer === "scene";
  const costsOpen = activeDrawer === "costs";
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

  // Group-level estimate.  Prefer the LLM-derived snapshot the server
  // stamped at submit (``total_estimated_cost_credits``); falls back
  // to summing per-task heuristic stamps for legacy groups submitted
  // before that field existed.  Without this fallback the headline
  // would diverge from what the user was quoted pre-render, since the
  // per-task stamps are heuristic-derived and the snapshot is the
  // LLM-derived number the user actually saw.
  const totalEstimatedCost = typeof job.total_estimated_cost_credits === "number"
    ? job.total_estimated_cost_credits
    : tasksList.reduce(
        (sum, task) => sum + (typeof task.estimated_cost_credits === "number" ? task.estimated_cost_credits : 0),
        0,
      );

  // Actual-cost rollup uses the polled per-task ``actual_cost_credits``
  // directly.  Server computes live values at request time (running
  // tasks substitute ``now``); each ~10s poll refreshes.
  const totalActualCost = tasksList.reduce(
    (sum, task) => sum + (typeof task.actual_cost_credits === "number" ? task.actual_cost_credits : 0),
    0,
  );
  // Server provides ``total_actual_cost_credits`` on the group payload
  // for terminal groups.  Prefer the live sum; fall back to the
  // server snapshot when the live sum is 0 (covers first render).
  const groupActualCost = totalActualCost > 0
    ? totalActualCost
    : (typeof job.total_actual_cost_credits === "number" ? job.total_actual_cost_credits : 0);

  return (
    <div className="jd-body jd-body-split">
      <div className="jd-detail-grid">
        <div className="jd-left-col">
          <div className="card jd-gauge-card">
            <GroupGauge pct={overallPct} color={gaugeColor} label={t("detail.complete")} />
            <div className="jd-gauge-info">
              <div className="jd-gauge-frames-lbl">{t("detail.frames")}</div>
              <div className="jd-gauge-frames-val">
                {total != null ? `${rendered} / ${total}` : `${rendered}`}
              </div>
              <div className="jd-gauge-bar">
                <div className="jd-gauge-bar-fill" style={{ width: `${overallPct ?? 0}%`, background: gaugeColor }} />
              </div>
              {totalEstimatedCost > 0 && (
                <div className="jd-gauge-cost">
                  <span className="jd-gauge-cost-lbl">{t("detail.estimatedCost")}</span>
                  <span className="jd-gauge-cost-val">{t("cost.approx", { credits: formatCredits(totalEstimatedCost) })}</span>
                </div>
              )}
            </div>
          </div>

          <div className="jd-stat-grid">
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>}
                label={t("detail.frames")} value={total != null ? `${rendered} / ${total}` : `${rendered}`}
              />
            </div>
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 14h.01M10 14h.01" /></svg>}
                label={t("detail.machines")} value={tasksLoading ? "…" : `${tasksDone} / ${taskCount}`}
              />
            </div>
            <div className="jd-stat-tile-card">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>}
                label={t("detail.outputFiles")} value={availableOutputCount}
              />
            </div>
            <div className="jd-stat-tile-card jd-stat-tile-card--fail">
              <StatTile
                icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" /></svg>}
                label={t("detail.failed")} value={tasksLoading ? "…" : (tasksList.filter((task) => task.status === "failed")).length}
              />
            </div>
          </div>

          {latestOutputFile ? (
            <div className="jd-preview-card">
              {/* Clickable — opens the latest frame in the full-size viewer,
                  same flow as clicking a tile in the frames gallery. */}
              <button
                type="button"
                className="jd-preview-thumb jd-preview-thumb--click"
                onClick={onOpenLatestFrame}
                disabled={!onOpenLatestFrame || !!openingFrameKey}
                title={latestOutputFile}
                aria-label={t("detail.latestFrame")}
              >
                <JobThumbnail job={job} authToken={authToken} backendUrl={backendUrl} />
                {!["done", "failed", "cancelled"].includes(job.status) && <div className="jd-preview-sweep" />}
                <span className="jd-preview-open-hint" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M15 3h6v6" />
                    <path d="M9 21H3v-6" />
                    <path d="M21 3l-7 7" />
                    <path d="M3 21l7-7" />
                  </svg>
                </span>
              </button>
              <div className="jd-preview-meta">
                <span className="jd-preview-name">{latestOutputFile}</span>
                <span className="jd-preview-tag">{t("detail.latestFrame")}</span>
              </div>
            </div>
          ) : isTerminalStatus(job.status) ? (
            // Terminal before any frame landed — nothing will ever load, so show
            // an empty state instead of a spinner that would hang forever.
            <div className="jd-preview-empty">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <circle cx="9" cy="9" r="2" />
                <path d="M21 15l-5-5L5 21" />
              </svg>
              <span>{t("detail.noPreview")}</span>
            </div>
          ) : (
            // Still rendering, first frame not in yet — a genuine wait, so spin.
            <div className="jd-preview-loader-wrap">
              <Loader />
            </div>
          )}

          <div className={`jd-actions${latestOutputFile ? "" : " jd-actions--centered"}`}>
            {canCancel && (
              <button className="btn btn-danger" onClick={onCancel} disabled={canceling}>
                {canceling ? t("detail.stopping") : t("detail.stopRender")}
              </button>
            )}
            {canDownloadAvailable && (
              <button className="btn btn-primary" onClick={onDownload} disabled={isDownloading}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>
                {isDownloading ? (downloadState?.progress || t("detail.downloading")) : job.status === "done" ? t("detail.downloadAll") : t("detail.downloadAvailable")}
              </button>
            )}
            <button className="btn btn-secondary" onClick={onRemove}>{t("common:actions.remove")}</button>
          </div>
        </div>

        <div className="jd-right-col">
          {job.status === "failed" && job.error && <div className="inst-error">{job.error}</div>}

          {tasksLoading ? (
            <div className="jd-section-loader">
              <Loader />
            </div>
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
                  <span>{t("detail.renderedFrames")}</span>
                  <span className="jd-frames-count">
                    {availableOutputCount}
                    {galleryOpen && countUniqueFrames(galleryState?.files) > 0 && ` (${countUniqueFrames(galleryState?.files)})`}
                  </span>
                  <svg className={`jd-frames-chevron${galleryOpen ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M6 9l6 6 6-6" /></svg>
                </button>
                {galleryOpen && onRefreshFrames && (
                  <button
                    type="button"
                    className={`jd-frames-refresh${galleryState?.loading ? " jd-frames-refresh--spinning" : ""}`}
                    onClick={onRefreshFrames}
                    disabled={!!galleryState?.loading}
                    title={galleryState?.loading ? t("common:actions.refreshing") : t("detail.refreshFrames")}
                    aria-label={t("detail.refreshFrames")}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
                      <path d="M21 3v5h-5" />
                    </svg>
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
            <span className="jd-drawer-panel-title">{t(`detail.drawers.${activeDrawer}`)}</span>
            <button
              type="button"
              className="jd-drawer-panel-close"
              onClick={() => setActiveDrawer(null)}
              title={t(DRAWER_HIDE_KEYS[activeDrawer])}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="jd-drawer-panel-body">
            {instancesOpen && (
              tasksLoading ? (
                <div className="jd-section-loader">
                  <Loader />
                </div>
              ) : tasksList.some((task) => ["done", "failed", "cancelled"].includes(task.status)) ? (
                <>
                  <VastInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="finished" />
                  <ModalInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="finished" />
                  <CommunityInstancePanel tasks={tasksList} backendUrl={backendUrl} onRefresh={onRefresh} mode="finished" />
                </>
              ) : (
                <div className="jd-drawer-empty">
                  <p className="muted">{t("detail.instancesEmpty")}</p>
                </div>
              )
            )}
            {sceneOpen && (
              <HeavinessPanel
                heaviness={job.heaviness ?? null}
                overrides={job.resolved_render_settings?.render ?? null}
                loading={tasksLoading}
              />
            )}
            {costsOpen && (
              <>
                <div className="jd-cost-summary">
                  <div className="jd-cost-summary-row">
                    <span className="jd-cost-summary-label">{t("detail.cost.estimated")}</span>
                    <span className="jd-cost-summary-value">
                      {tasksLoading ? "…" : totalEstimatedCost > 0 ? t("cost.approx", { credits: formatCredits(totalEstimatedCost) }) : "—"}
                    </span>
                  </div>
                  <div className="jd-cost-summary-row">
                    <span className="jd-cost-summary-label">{t("detail.cost.actual")}</span>
                    <span className="jd-cost-summary-value">
                      {tasksLoading ? "…" : groupActualCost > 0 ? t("cost.exact", { credits: formatCredits(groupActualCost) }) : "—"}
                    </span>
                  </div>
                </div>

                {tasksLoading ? (
                  <div className="jd-cost-empty">
                    <Loader size="sm" />
                  </div>
                ) : tasksList.length > 0 ? (
                  <ul className="jd-cost-list">
                    {tasksList
                      .slice()
                      .sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0))
                      .map((task) => {
                        const range = task.frame_start != null && task.frame_end != null
                          ? `${task.frame_start}–${task.frame_end}`
                          : "—";
                        const est = typeof task.estimated_cost_credits === "number"
                          ? t("cost.approx", { credits: formatCredits(task.estimated_cost_credits) })
                          : "—";
                        const actual = typeof task.actual_cost_credits === "number"
                          ? t("cost.exact", { credits: formatCredits(task.actual_cost_credits) })
                          : "—";
                        return (
                          <li key={task.job_id} className="jd-cost-item">
                            <span className="jd-cost-item-range">{range}</span>
                            <span className="jd-cost-item-machine" title={task.machine_gpu}>
                              {task.machine_gpu || t("detail.unassigned")}
                            </span>
                            <span className="jd-cost-item-value jd-cost-item-est">{est}</span>
                            <span className="jd-cost-item-value">{actual}</span>
                          </li>
                        );
                      })}
                  </ul>
                ) : (
                  <div className="jd-cost-empty">
                    {t("detail.costsEmpty")}
                  </div>
                )}
              </>
            )}
          </div>
        </aside>
      )}

      <nav className={`jd-dock${activeDrawer ? " jd-dock-shifted" : ""}`} aria-label={t("detail.dock.ariaLabel")}>
        <button
          type="button"
          className={`jd-dock-btn${instancesOpen ? " jd-dock-btn-active" : ""}`}
          onClick={() => toggleDrawer("instances")}
          title={instancesOpen ? t("detail.dock.hideInstances") : t("detail.dock.showInstances")}
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
          title={sceneOpen ? t("detail.dock.hideScene") : t("detail.dock.showScene")}
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
          title={costsOpen ? t("detail.dock.hideCosts") : t("detail.dock.showCosts")}
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
  const { t } = useTranslation(["myJobs", "common"]);
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
          <BigGauge pct={progressPct} color={gaugeColor} label={t("detail.complete")} />
          <div className="jd-gauge-stats">
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>}
              label={t("detail.frames")} value={hasTotalFrames ? `${renderedFrames} / ${totalFrames}` : `${renderedFrames}`}
            />
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>}
              label={t("detail.outputFiles")} value={availableOutputCount}
            />
          </div>
        </div>
      </div>

      {job.status === "failed" && job.error && <div className="inst-error">{job.error}</div>}

      {/* Frames — collapsible */}
      {canDownloadAvailable && (
        <div className="jd-frames-section">
          <div className="jd-frames-header">
            <button type="button" className="jd-frames-toggle" onClick={onToggleGallery}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>
              <span>{t("detail.renderedFrames")}</span>
              <span className="jd-frames-count">
                {availableOutputCount}
                {galleryOpen && countUniqueFrames(galleryState?.files) > 0 && ` (${countUniqueFrames(galleryState?.files)})`}
              </span>
              <svg className={`jd-frames-chevron${galleryOpen ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M6 9l6 6 6-6" /></svg>
            </button>
            {galleryOpen && onRefreshFrames && (
              <button
                type="button"
                className={`jd-frames-refresh${galleryState?.loading ? " jd-frames-refresh--spinning" : ""}`}
                onClick={onRefreshFrames}
                disabled={!!galleryState?.loading}
                title={galleryState?.loading ? t("common:actions.refreshing") : t("detail.refreshFrames")}
                aria-label={t("detail.refreshFrames")}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
                  <path d="M21 3v5h-5" />
                </svg>
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
            {downloadingId === id ? t("detail.downloading") : job.status === "done" ? t("detail.download") : t("detail.downloadAvailable")}
          </button>
        )}
        <button className="btn btn-secondary" onClick={onRemove}>{t("common:actions.remove")}</button>
      </div>
    </div>
  );
}

export default function JobDetailView({
  job, backendUrl, authToken, tasksLoading, downloadState, downloadingId, canceling,
  galleryOpen, galleryState, openingFrameKey, refreshing,
  onBack, onDownload, onCancel, onToggleGallery, onOpenFrame, onOpenLatestFrame, onRemove,
  onRefresh, onRefreshFrames,
}) {
  const { t } = useTranslation(["myJobs", "common"]);
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

  // Accrued cost.  Same formula RenderGroupDetail uses internally,
  // hoisted to the wrapper so the floating top-right pill stays in sync
  // with the per-machine cost chips and persists into terminal status
  // Cost rollup reads polled ``actual_cost_credits`` directly.
  // Server applies the priority multiplier + USD->credits conversion
  // at the wire boundary; we just sum and display.
  const costTasks = isGroup ? (job?.tasks || []) : (job ? [job] : []);
  const liveCostSum = costTasks.reduce(
    (sum, task) => sum + (typeof task?.actual_cost_credits === "number" ? task.actual_cost_credits : 0),
    0,
  );
  const liveCost = liveCostSum > 0
    ? liveCostSum
    : isGroup
      ? (typeof job?.total_actual_cost_credits === "number" ? job.total_actual_cost_credits : 0)
      : (typeof job?.actual_cost_credits === "number" ? job.actual_cost_credits : 0);
  // Label flips by status so terminal jobs read "TOTAL COST" / "FINAL COST"
  // instead of the live-active "LIVE COST".  Same value either way --
  // it's the actual cost as of now (or end-of-render for terminal).
  const costLabel = status === "done"
    ? t("detail.costLabel.total")
    : status === "failed" || status === "cancelled"
      ? t("detail.costLabel.final")
      : t("detail.costLabel.live");

  return (
    <div className="job-detail">
      <div className="job-detail-header">
        <div className="jd-header-top" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button type="button" className="job-detail-back" onClick={onBack}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M19 12H5M12 5l-7 7 7 7" /></svg>
            {t("detail.allJobs")}
          </button>
          <button
            type="button"
            className="job-detail-back"
            onClick={onRefresh}
            disabled={!!refreshing}
            style={{ opacity: refreshing ? 0.5 : 1 }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></svg>
            {refreshing ? t("common:actions.refreshing") : t("common:actions.refresh")}
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
                {jobStatusLabel(status)}
              </span>
              {(() => {
                // Priority surfaces from any task row (every chunk stamps
                // the group's priority).  Show only when it's not NORMAL
                // (1) -- a NORMAL render is the default and the badge
                // would be noise.
                const p = costTasks[0]?.priority;
                if (p === 0) {
                  return <span className="priority-badge priority-low">{t("detail.priority.low")}</span>;
                }
                if (p === 2) {
                  return <span className="priority-badge priority-high">{t("detail.priority.high")}</span>;
                }
                return null;
              })()}
              <span className="job-detail-meta-chip">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 14h.01M10 14h.01" /></svg>
                {t("machineCount", { count: taskCount })}
              </span>
              <span className="job-detail-id">{id ? id.slice(0, 12) + "..." : ""}</span>
              {job?.submitted_at && (
                <span className="job-detail-date">
                  {new Date(job.submitted_at).toLocaleDateString()} {new Date(job.submitted_at).toLocaleTimeString()}
                </span>
              )}
            </div>
          </div>
          <div className="jd-live-cost" title={t("detail.accruedCostTitle")}>
            <span className="jd-live-cost-label">
              {!["done", "failed", "cancelled"].includes(status) && <span className="jd-live-cost-dot" />}
              {costLabel}
            </span>
            <span className="jd-live-cost-value">{t("cost.exact", { credits: formatCredits(liveCost) })}</span>
          </div>
        </div>
      </div>

      {isGroup ? (
        <RenderGroupDetail
          job={job} backendUrl={backendUrl} authToken={authToken} tasksLoading={tasksLoading}
          downloadState={downloadState} downloadingId={downloadingId} canceling={canceling}
          galleryOpen={galleryOpen} galleryState={galleryState} openingFrameKey={openingFrameKey}
          onDownload={onDownload} onCancel={onCancel} onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame} onOpenLatestFrame={onOpenLatestFrame} onRemove={onRemove}
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
