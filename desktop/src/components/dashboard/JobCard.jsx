import { pauseAgent, resumeAgent, stopJob } from "../../services/sidecar";

/* Decorative render viewport from the Aurora Glass prototype — grid floor,
   spinning ember cube, scan sweep.  Pure CSS chrome; the HUD chips carry the
   real state. */
function RenderViewport({ rendering, hudRight, hudBottom }) {
  return (
    <div className="dash-viewport">
      <div className="dash-viewport-grid" />
      <div className="dash-viewport-shadow" />
      <div className="dash-cube">
        <div className="dash-cube-face dash-cube-front" />
        <div className="dash-cube-face dash-cube-back" />
        <div className="dash-cube-face dash-cube-right" />
        <div className="dash-cube-face dash-cube-left" />
        <div className="dash-cube-face dash-cube-top" />
        <div className="dash-cube-face dash-cube-bottom" />
      </div>
      {rendering && <div className="dash-viewport-sweep" />}
      <div className="dash-viewport-hud dash-viewport-hud-tl">
        <span className={`dash-hud-dot${rendering ? " live" : ""}`} />
        {rendering ? "RENDERING" : "IDLE"}
      </div>
      {hudRight && <div className="dash-viewport-hud dash-viewport-hud-tr">{hudRight}</div>}
      {hudBottom && <div className="dash-viewport-hud dash-viewport-hud-bl">{hudBottom}</div>}
    </div>
  );
}

export default function JobCard({ currentJob, status }) {
  if (!currentJob) {
    if (status === "connected") {
      return (
        <div className="card dash-hero">
          <div className="dash-hero-chips">
            <span className="dash-chip dash-chip-soft">
              <span className="dash-chip-dot" />
              STANDING BY
            </span>
            <span className="dash-chip dash-chip-hair">WAITING FOR RENDER JOBS</span>
          </div>
          <RenderViewport rendering={false} hudBottom="viewport preview" />
          <p className="dash-hero-idle-note">
            Waiting for render jobs — your machine is registered and will pick
            up work automatically.
          </p>
        </div>
      );
    }
    return null;
  }

  const totalFrames =
    typeof currentJob.total_frames === "number" ? currentJob.total_frames : null;
  const renderedFrames =
    typeof currentJob.rendered_frames === "number" ? currentJob.rendered_frames : 0;
  const progressPct =
    typeof currentJob.progress_pct === "number"
      ? Math.max(0, Math.min(100, currentJob.progress_pct))
      : null;
  const hasTotalFrames = typeof totalFrames === "number" && totalFrames > 0;
  const paused = status === "paused";

  return (
    <div className="card dash-hero dash-hero--active">
      <div className="dash-hero-chips">
        <span className="dash-chip dash-chip-soft">
          <span className={`dash-chip-dot${paused ? "" : " live"}`} />
          {paused ? "PAUSED" : "LIVE RENDER"}
        </span>
        <span className="dash-chip dash-chip-hair">JOB {currentJob.job_id}</span>
      </div>

      <div className="dash-hero-headline">
        <span className="dash-hero-pct">
          {progressPct !== null ? Math.round(progressPct) : "—"}
        </span>
        <span className="dash-hero-pct-sign">%</span>
        <div className="dash-hero-file">
          <div className="dash-hero-filename">{currentJob.filename}</div>
          <div className="dash-hero-meta">
            {hasTotalFrames
              ? `FRAME ${Math.min(renderedFrames, totalFrames)} / ${totalFrames}`
              : "PREPARING RENDER…"}
          </div>
        </div>
      </div>

      <div className={`dash-hero-bar${progressPct === null ? " indeterminate" : ""}`}>
        <div
          className="dash-hero-bar-fill"
          style={{ width: `${progressPct ?? 100}%` }}
        />
      </div>

      <RenderViewport
        rendering={!paused}
        hudRight={
          typeof currentJob.current_frame === "number"
            ? `FRAME ${currentJob.current_frame}`
            : null
        }
        hudBottom="viewport preview"
      />

      <div className="dash-hero-foot">
        <div className="dash-hero-stat">
          <div className="dash-hero-stat-label">RENDERED</div>
          <div className="dash-hero-stat-value">
            {hasTotalFrames ? `${Math.min(renderedFrames, totalFrames)} / ${totalFrames}` : "—"}
          </div>
        </div>
        {typeof currentJob.current_frame === "number" && (
          <div className="dash-hero-stat">
            <div className="dash-hero-stat-label">CURRENT FRAME</div>
            <div className="dash-hero-stat-value">{currentJob.current_frame}</div>
          </div>
        )}
        <div className="dash-hero-actions">
          {paused ? (
            <button className="btn-hero-outline" onClick={resumeAgent}>
              Resume
            </button>
          ) : (
            <button className="btn-hero-outline" onClick={pauseAgent}>
              Pause
            </button>
          )}
          <button className="btn btn-danger" onClick={stopJob}>
            Stop
          </button>
        </div>
      </div>
    </div>
  );
}
