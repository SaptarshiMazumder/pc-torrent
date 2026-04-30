import {
  STATUS_LABELS,
  isTerminalStatus,
  terminalFallbackPct,
  resolveJobFilename,
} from "../../utils/jobUtils";
import SegmentedProgressBar from "./SegmentedProgressBar";
import VastInstancePanel from "./VastInstancePanel";
import ModalInstancePanel from "../ModalInstancePanel";
import CommunityInstancePanel from "./CommunityInstancePanel";
import FrameGalleryPanel from "./FrameGalleryPanel";
import HeavinessPanel from "./HeavinessPanel";
import FailedChunksPanel from "./FailedChunksPanel";

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
  job, backendUrl, authToken, downloadState, downloadingId, canceling,
  galleryOpen, galleryState, openingFrameKey,
  onDownload, onCancel, onToggleGallery, onOpenFrame, onRemove, onReRender,
  onRefresh,
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

  return (
    <div className="jd-body">
      {/* Top grid: gauge + stats + progress */}
      <div className="jd-top-grid">
        <div className="jd-card jd-card-gauge">
          <BigGauge pct={overallPct} color={gaugeColor} label="COMPLETE" />
          <div className="jd-gauge-stats">
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>}
              label="Frames" value={total != null ? `${rendered} / ${total}` : `${rendered}`}
            />
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 14h.01M10 14h.01" /></svg>}
              label="Machines" value={`${tasksDone} / ${taskCount} done`}
            />
            <StatTile
              icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></svg>}
              label="Output Files" value={availableOutputCount}
            />
          </div>
        </div>

        {taskCount > 0 && (
          <div className="jd-card jd-card-progress">
            <div className="jd-card-label">Render Progress</div>
            <SegmentedProgressBar tasks={job.tasks} totalFrames={job.total_frames} />
          </div>
        )}
      </div>

      {/* Errors & download status */}
      {job.status === "failed" && job.error && <div className="inst-error">{job.error}</div>}
      {downloadState?.status === "done" && (
        <div className="rentee-job-success">Downloaded to <code>{downloadState.path}</code>{downloadState?.summary ? ` (${downloadState.summary})` : ""}</div>
      )}
      {downloadState?.status === "error" && <div className="inst-error">{downloadState.error}</div>}

      {/* Stuck chunks the user can manually re-dispatch */}
      <FailedChunksPanel tasks={job.tasks || []} backendUrl={backendUrl} onRefresh={onRefresh} />

      {/* GPU Instances */}
      <VastInstancePanel tasks={job.tasks || []} backendUrl={backendUrl} />
      <ModalInstancePanel tasks={job.tasks || []} backendUrl={backendUrl} />
      <CommunityInstancePanel tasks={job.tasks || []} backendUrl={backendUrl} />

      {/* Scene heaviness — async-friendly: skeleton until analysis_snapshot lands. */}
      <HeavinessPanel
        heaviness={job.analysis_snapshot?.heaviness ?? null}
        loading={!job.analysis_snapshot}
      />

      {/* Frames — collapsible, below GPU instances */}
      {canViewFrames && (
        <div className="jd-frames-section">
          <button type="button" className="jd-frames-toggle" onClick={onToggleGallery}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>
            <span>Rendered Frames</span>
            <span className="jd-frames-count">{availableOutputCount}</span>
            <svg className={`jd-frames-chevron${galleryOpen ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M6 9l6 6 6-6" /></svg>
          </button>
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

      {/* Actions */}
      <div className="jd-actions">
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
        <button className="btn btn-secondary" onClick={onReRender}>Re-render</button>
        <button className="btn btn-secondary" onClick={onRemove}>Remove</button>
      </div>
    </div>
  );
}

function SingleJobDetail({
  job, backendUrl, authToken, downloadState, downloadingId,
  galleryOpen, galleryState, openingFrameKey,
  onDownload, onToggleGallery, onOpenFrame, onRemove,
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
          <button type="button" className="jd-frames-toggle" onClick={onToggleGallery}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>
            <span>Rendered Frames</span>
            <span className="jd-frames-count">{availableOutputCount}</span>
            <svg className={`jd-frames-chevron${galleryOpen ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M6 9l6 6 6-6" /></svg>
          </button>
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
  job, backendUrl, authToken, downloadState, downloadingId, canceling,
  galleryOpen, galleryState, openingFrameKey,
  onBack, onDownload, onCancel, onToggleGallery, onOpenFrame, onRemove, onReRender,
  onRefresh,
}) {
  const isGroup = !!job?.group_id;
  const displayName = resolveJobFilename(job);
  const status = job?.status || "pending";
  const id = job?.group_id || job?.job_id || "";
  const taskCount = isGroup ? (job.tasks || []).length : 1;

  return (
    <div className="job-detail">
      <div className="job-detail-header">
        <div className="jd-header-top">
          <button type="button" className="job-detail-back" onClick={onBack}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M19 12H5M12 5l-7 7 7 7" /></svg>
            All Jobs
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
          job={job} backendUrl={backendUrl} authToken={authToken}
          downloadState={downloadState} downloadingId={downloadingId} canceling={canceling}
          galleryOpen={galleryOpen} galleryState={galleryState} openingFrameKey={openingFrameKey}
          onDownload={onDownload} onCancel={onCancel} onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame} onRemove={onRemove} onReRender={onReRender}
          onRefresh={onRefresh}
        />
      ) : (
        <SingleJobDetail
          job={job} backendUrl={backendUrl} authToken={authToken}
          downloadState={downloadState} downloadingId={downloadingId}
          galleryOpen={galleryOpen} galleryState={galleryState} openingFrameKey={openingFrameKey}
          onDownload={onDownload} onToggleGallery={onToggleGallery}
          onOpenFrame={onOpenFrame} onRemove={onRemove}
        />
      )}
    </div>
  );
}
