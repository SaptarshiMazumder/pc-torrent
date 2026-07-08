import { useEffect, useMemo, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { useTranslation } from "react-i18next";
import { useAuth } from "../contexts/AuthContext";
import { getFirebaseToken } from "../services/api";
import { aggregateLive, aggregateLifetime } from "../utils/telemetryDerive";
import {
  jobKey,
  jobProgressPct,
  jobTotalFrames,
  jobRenderedFrames,
  jobCostCredits,
  jobSubmittedMs,
  jobEngineLabel,
  jobSamples,
  resolveJobFilename,
  jobResolutionLabel,
  formatDurationLabel,
  formatDateTimeLabel,
  formatCompactNumber,
} from "../utils/jobUtils";
import { formatCredits } from "../utils/creditsFormat";
import JobThumbnail from "../components/jobs/JobThumbnail";

// ---- Icons (Lucide-style, 2px stroke, currentColor) ----
function IconFrames() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="14" rx="2" />
      <path d="M3 9h18M8 4v14M16 4v14" />
    </svg>
  );
}
function IconMachines() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <rect x="9" y="9" width="6" height="6" />
    </svg>
  );
}
function IconQueue() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}
function IconActivity() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </svg>
  );
}
function IconStack() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2 2 7l10 5 10-5-10-5Z" />
      <path d="m2 17 10 5 10-5M2 12l10 5 10-5" />
    </svg>
  );
}
function IconClock() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}
function IconCoins() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6" />
      <path d="M18.09 10.37A6 6 0 1 1 10.34 18" />
      <path d="M7 6h1v4" />
      <path d="m16.71 13.88.7.71-2.82 2.82" />
    </svg>
  );
}
function IconMoon() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
    </svg>
  );
}
function IconServer() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="7" rx="2" />
      <rect x="3" y="13" width="18" height="7" rx="2" />
      <path d="M7 7.5h.01M7 16.5h.01" />
    </svg>
  );
}

// Decorative render viewport — grid floor + spinning ember cube + scan sweep.
// Purely a "rendering" animation; the real frame lives in the NOW tile beside it.
function CubeViewport({ rendering, topRight, bottomLeft }) {
  return (
    <div className="dash-viewport dash-viewport--cube">
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
        {rendering ? "RENDERING" : "PAUSED"}
      </div>
      {topRight && <div className="dash-viewport-hud dash-viewport-hud-tr">{topRight}</div>}
      {bottomLeft && <div className="dash-viewport-hud dash-viewport-hud-bl">{bottomLeft}</div>}
    </div>
  );
}

// One unified stat card — centered, icon has no background fill.
function StatCard({ tone, Icon, value, label }) {
  return (
    <div className="dash-stat">
      <span className={`dash-stat-icon dash-stat-icon--${tone}`}><Icon /></span>
      <div className="dash-stat-value">{value}</div>
      <div className="dash-stat-label">{label}</div>
    </div>
  );
}

const FLEET_TONES = ["teal", "blue", "violet", "green", "amber"];

const STATUS_TO_BADGE = {
  running: "status-running",
  pending: "status-pending",
  uploading: "status-uploading",
  done: "status-done",
  failed: "status-failed",
  cancelled: "status-cancelled",
};
const STATUS_BADGE_LABELS = {
  running: "Rendering",
  pending: "Queued",
  uploading: "Uploading",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

function recentBarColor(status) {
  if (status === "done") return "rgb(18, 161, 80)";
  if (status === "failed") return "rgb(229, 72, 77)";
  if (status === "pending" || status === "uploading") return "rgb(63, 116, 255)";
  return "";
}

function greeting() {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

const ACTIVITY_PERIODS = [7, 30, 90];

// Render activity over a selectable window — frames delivered per day, from the
// lifetime day-buckets the app already derives.
function RenderActivity({ overTime }) {
  const [days, setDays] = useState(30);
  const data = useMemo(() => {
    const cutoff = Date.now() - days * 86400000;
    return overTime.filter((b) => b.ts >= cutoff);
  }, [overTime, days]);
  const totalFrames = data.reduce((s, b) => s + (b.frames || 0), 0);
  const totalLabel = formatCompactNumber(totalFrames);

  return (
    <div className="card dash-activity-card">
      <div className="dash-panel-head">
        <span className="dash-panel-title">Render activity</span>
        <div className="dash-period-tabs">
          {ACTIVITY_PERIODS.map((d) => (
            <button
              key={d}
              type="button"
              className={`dash-period-tab${days === d ? " active" : ""}`}
              onClick={() => setDays(d)}
            >
              {d}D
            </button>
          ))}
        </div>
      </div>
      <div className="dash-activity-metric">
        <span className="dash-activity-value">{totalLabel === "—" ? "0" : totalLabel}</span>
        <span className="dash-activity-unit">frames · last {days}d</span>
      </div>
      {data.length === 0 ? (
        <div className="dash-activity-empty">No render activity in the last {days} days.</div>
      ) : (
        <ResponsiveContainer width="100%" height={158}>
          <AreaChart data={data} margin={{ top: 6, right: 6, left: -18, bottom: 0 }}>
            <defs>
              <linearGradient id="dash-act-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(238, 90, 41)" stopOpacity={0.32} />
                <stop offset="100%" stopColor="rgb(238, 90, 41)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(154, 141, 123, 0.28)" strokeWidth={0.6} vertical={false} />
            <XAxis
              dataKey="label"
              tick={{ fill: "var(--muted)", fontSize: 9, fontFamily: "IBM Plex Mono, monospace", fontWeight: 600 }}
              axisLine={{ stroke: "rgba(154, 141, 123, 0.28)" }}
              tickLine={false}
              minTickGap={30}
            />
            <YAxis
              tick={{ fill: "var(--muted)", fontSize: 9, fontFamily: "IBM Plex Mono, monospace", fontWeight: 600 }}
              axisLine={false}
              tickLine={false}
              width={30}
            />
            <Tooltip
              contentStyle={{ background: "var(--bg-primary)", border: "1px solid var(--gbrd)", borderRadius: 12, fontSize: 12, boxShadow: "var(--card-shadow)" }}
              labelStyle={{ color: "var(--muted)", fontFamily: "IBM Plex Mono, monospace", fontSize: 10 }}
              itemStyle={{ color: "rgb(238, 90, 41)" }}
            />
            <Area
              type="monotone"
              dataKey="frames"
              name="Frames"
              stroke="rgb(238, 90, 41)"
              strokeWidth={2.5}
              strokeLinecap="round"
              fill="url(#dash-act-grad)"
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

export default function HomePage({
  ongoingJobs,
  pastJobs,
  loadingOngoing,
  loadingPast,
  onRefresh,
  onNavigate,
  backendUrl,
}) {
  const { t } = useTranslation("common");
  const { user } = useAuth();
  const [authToken, setAuthToken] = useState("");

  useEffect(() => {
    if (!ongoingJobs.length && !pastJobs.length && !loadingOngoing && !loadingPast) {
      onRefresh?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const live = useMemo(() => aggregateLive(ongoingJobs), [ongoingJobs]);
  const allJobs = useMemo(() => [...ongoingJobs, ...pastJobs], [ongoingJobs, pastJobs]);
  const lifetime = useMemo(() => aggregateLifetime(allJobs), [allJobs]);

  const heroJob = useMemo(() => {
    const running = ongoingJobs.find((j) => j.status === "running");
    return running || ongoingJobs[0] || null;
  }, [ongoingJobs]);

  const queuedCount = useMemo(
    () => ongoingJobs.filter((j) => j.status === "pending" || j.status === "uploading").length,
    [ongoingJobs],
  );

  const recentJobs = useMemo(
    () => [...ongoingJobs, ...pastJobs].slice(0, 6),
    [ongoingJobs, pastJobs],
  );

  const maxFleetChunks = live.gpuUsage.reduce((m, g) => Math.max(m, g.chunks), 0) || 1;

  const firstName =
    (user?.displayName || "").trim().split(/\s+/)[0] ||
    (user?.email || "").split("@")[0] ||
    "";

  const heroRunning = heroJob?.status === "running";
  const heroPct = heroJob ? Math.round(jobProgressPct(heroJob)) : 0;
  const heroRendered = heroJob ? jobRenderedFrames(heroJob) : 0;
  const heroTotal = heroJob ? jobTotalFrames(heroJob) : 0;
  const heroSpent = heroJob ? jobCostCredits(heroJob) : 0;
  const heroStartedMs = heroJob ? jobSubmittedMs(heroJob) : 0;
  const heroElapsed = heroStartedMs
    ? formatDurationLabel(Math.max(1, Math.floor((Date.now() - heroStartedMs) / 1000)))
    : "";
  const heroEngine = heroJob ? jobEngineLabel(heroJob) : "";
  const heroSamples = heroJob ? jobSamples(heroJob) : null;
  const heroRes = heroJob ? jobResolutionLabel(heroJob) : "";
  const heroGpus = heroJob ? (live.gpuUsage.length || null) : null;
  const hasLiveFleet = live.gpuUsage.length > 0;

  // Recent-frames filmstrip.  Fixed-width slots so a single frame never
  // stretches the whole row: we reserve up to FILM_MAX slots and fill the ones
  // that have rendered (the rest are dashed "upcoming" placeholders).  Only
  // shown when the job is big enough to warrant it (>= FILM_MIN_TOTAL frames);
  // otherwise the strip is left blank.
  const FILM_MAX = 6;
  const FILM_MIN_TOTAL = 5;
  const filmSlots = [];
  if (heroTotal >= FILM_MIN_TOTAL && heroRendered > 0) {
    const slots = Math.min(FILM_MAX, heroTotal);
    const end = Math.min(heroTotal, Math.max(slots, heroRendered));
    const start = end - slots + 1;
    for (let f = start; f <= end; f += 1) {
      filmSlots.push({ frame: f, filled: f <= heroRendered });
    }
  }

  return (
    <div className="page dashboard-page">
      <div className="page-eyebrow">{t("eyebrow.home")}</div>
      <h2>{firstName ? `${greeting()}, ${firstName}` : greeting()}</h2>

      {/* ============ LIVE NOW ============ */}
      <div className="dash-section-label">Live now</div>
      <div className="dash-stats">
        <StatCard
          tone="blue"
          Icon={IconFrames}
          value={live.totalFramesInFlight > 0 ? live.framesInFlight.toLocaleString() : "0"}
          label="Frames in flight"
        />
        <StatCard tone="teal" Icon={IconMachines} value={live.activeChunks || "0"} label="Machines live" />
        <StatCard tone="amber" Icon={IconQueue} value={queuedCount || "0"} label="In queue" />
        <StatCard tone="ember" Icon={IconActivity} value={ongoingJobs.length || "0"} label="Active renders" />
      </div>

      <div className="dash-hero-row">
        {heroJob ? (
          <div className="card dash-hero dash-hero--active">
            <div className="dash-hero-chips">
              <span className="dash-chip dash-chip-soft">
                <span className={`dash-chip-dot${heroRunning ? " live" : ""}`} />
                {heroRunning ? "LIVE RENDER" : (STATUS_BADGE_LABELS[heroJob.status] || heroJob.status)}
              </span>
              {heroEngine !== "—" && (
                <span className="dash-chip dash-chip-hair">
                  {heroEngine}
                  {heroSamples ? ` · ${heroSamples} spp` : ""}
                </span>
              )}
              {heroGpus ? (
                <span className="dash-chip dash-chip-hair">{heroGpus} GPU{heroGpus === 1 ? "" : "s"}</span>
              ) : null}
            </div>

            <div className="dash-hero-headline">
              <span className="dash-hero-pct">{heroPct}</span>
              <span className="dash-hero-pct-sign">%</span>
              <div className="dash-hero-file">
                <div className="dash-hero-filename">{resolveJobFilename(heroJob)}</div>
                <div className="dash-hero-meta">
                  {heroTotal > 0 ? `FRAME ${heroRendered} / ${heroTotal}` : "PREPARING…"}
                </div>
              </div>
            </div>

            <div className="dash-hero-bar">
              <div className="dash-hero-bar-fill" style={{ width: `${heroPct}%` }} />
            </div>

            {/* Render graphic: decorative cube viewport + real latest frame + filmstrip */}
            <div className="dash-media">
              <div className="dash-media-row">
                <CubeViewport
                  rendering={heroRunning}
                  topRight={heroSamples ? `${heroSamples} SPP` : (heroEngine !== "—" ? heroEngine : null)}
                  bottomLeft={heroEngine !== "—" ? `preview · ${heroEngine}` : "preview"}
                />
                <div className="dash-now-tile">
                  <JobThumbnail job={heroJob} authToken={authToken} backendUrl={backendUrl} />
                  <span className="dash-now-badge">NOW</span>
                  <div className="dash-now-label">
                    <div className="dash-now-frame">
                      {heroRendered > 0 ? `FRAME ${String(heroRendered).padStart(4, "0")}` : "AWAITING FRAME"}
                    </div>
                    <div className="dash-now-sub">
                      latest{heroRes && heroRes !== "—" ? ` · ${heroRes}` : ""}
                    </div>
                  </div>
                </div>
              </div>
              {filmSlots.length > 0 && (
                <div
                  className="dash-filmstrip"
                  style={{ gridTemplateColumns: `repeat(${filmSlots.length}, 1fr)` }}
                >
                  {filmSlots.map((slot) => (
                    <div
                      key={slot.frame}
                      className={`dash-film-tile${slot.filled ? "" : " dash-film-tile--empty"}`}
                    >
                      {slot.filled && (
                        <span className="dash-film-num">{String(slot.frame).padStart(4, "0")}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="dash-hero-foot">
              <div className="dash-hero-stat">
                <div className="dash-hero-stat-label">SPENT</div>
                <div className="dash-hero-stat-value">{heroSpent > 0 ? `${formatCredits(heroSpent)} cr` : "—"}</div>
              </div>
              <div className="dash-hero-stat">
                <div className="dash-hero-stat-label">STARTED</div>
                <div className="dash-hero-stat-value">{heroElapsed ? `${heroElapsed} ago` : "—"}</div>
              </div>
              <div className="dash-hero-actions">
                <button className="btn-hero-outline" onClick={() => onNavigate?.("myjobs")}>Open job →</button>
              </div>
            </div>
          </div>
        ) : (
          <div className="card dash-hero dash-hero--idle">
            <div className="dash-hero-chips">
              <span className="dash-chip dash-chip-hair">
                <span className="dash-chip-dot" style={{ background: "var(--muted)" }} />
                NO ACTIVE RENDER
              </span>
            </div>
            {/* The checkered viewport box only wraps the "screen" — the hero
                card itself stays glass. No cube, just a sleeping state. */}
            <div className="dash-media dash-media--idle">
              <div className="dash-viewport dash-viewport--idle">
                <div className="dash-viewport-grid" />
                <div className="dash-idle-inner">
                  <span className="dash-idle-icon"><IconMoon /></span>
                  <div className="dash-idle-title">Fleet idle — nothing rendering</div>
                  <div className="dash-idle-sub">
                    Drop a .blend or .zip and start a render — the live preview
                    and frames will show up here.
                  </div>
                  <button className="btn btn-primary" onClick={() => onNavigate?.("create")}>
                    Create render →
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="dash-side">
          {/* Fleet — LIVE machines rendering your jobs right now. */}
          <div className="card dash-fleet-card">
            <div className="dash-panel-head">
              <span className="dash-panel-title">Fleet</span>
              {hasLiveFleet && (
                <span className="dash-fleet-live">
                  <span className="dash-fleet-live-dot" />
                  {live.activeChunks} forging
                </span>
              )}
            </div>

            {hasLiveFleet ? (
              <>
                <div className="dash-fleet-summary">
                  <div className="dash-fleet-sum">
                    <div className="dash-fleet-sum-val">{live.activeChunks}</div>
                    <div className="dash-fleet-sum-lbl">GPUs</div>
                  </div>
                  <div className="dash-fleet-sum">
                    <div className="dash-fleet-sum-val">{live.gpuUsage.length}</div>
                    <div className="dash-fleet-sum-lbl">Types</div>
                  </div>
                  <div className="dash-fleet-sum">
                    <div className="dash-fleet-sum-val">
                      {live.liveCredits > 0 ? formatCredits(live.liveCredits) : "0"}
                    </div>
                    <div className="dash-fleet-sum-lbl">Live cr</div>
                  </div>
                </div>
                <div className="dash-fleet-rows">
                  {live.gpuUsage.slice(0, 5).map((g, i) => (
                    <div key={g.name} className="dash-fleet-row">
                      <div className="dash-fleet-row-head">
                        <span className="dash-fleet-name">
                          <span className={`dash-spec-dot dash-spec-dot--${FLEET_TONES[i % FLEET_TONES.length]}`} />
                          {g.name}
                        </span>
                        <span className="dash-fleet-count">
                          {g.chunks} GPU{g.chunks === 1 ? "" : "s"} · {formatDurationLabel(g.seconds)}
                        </span>
                      </div>
                      <div className={`dash-fleet-bar dash-fleet-bar--${FLEET_TONES[i % FLEET_TONES.length]}`}>
                        <span style={{ width: `${Math.max(8, (g.chunks / maxFleetChunks) * 100)}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
                <button className="dash-fleet-footer-btn" onClick={() => onNavigate?.("available")}>
                  View all machines
                </button>
              </>
            ) : (
              <div className="dash-fleet-none">
                <span className="dash-fleet-none-icon"><IconServer /></span>
                <div className="dash-fleet-none-title">No machines working</div>
                <div className="dash-fleet-none-sub">
                  GPUs rendering your jobs appear here while a render is live.
                </div>
                <button className="dash-fleet-footer-btn" onClick={() => onNavigate?.("available")}>
                  Browse machines
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ============ ALL TIME ============ */}
      <div className="dash-section-label">All time</div>
      <div className="dash-stats">
        <StatCard tone="ember" Icon={IconStack} value={lifetime.totalRenders.toLocaleString()} label="Total renders" />
        <StatCard tone="teal" Icon={IconFrames} value={formatCompactNumber(lifetime.framesRendered) === "—" ? "0" : formatCompactNumber(lifetime.framesRendered)} label="Frames rendered" />
        <StatCard tone="violet" Icon={IconClock} value={lifetime.funFacts?.avgDurationSec ? formatDurationLabel(lifetime.funFacts.avgDurationSec) : "—"} label="Avg render time" />
        <StatCard tone="green" Icon={IconCoins} value={lifetime.creditsSpent > 0 ? formatCredits(lifetime.creditsSpent) : "0"} label="Credits spent" />
      </div>

      {/* ============ ACTIVITY + RECENT JOBS ============ */}
      <div className="dash-bottom-row">
        <RenderActivity overTime={lifetime.overTime} />

        <div className="card dash-recent-card">
          <div className="dash-panel-head">
            <span className="dash-panel-title">Recent jobs</span>
            <button className="dash-recent-all" onClick={() => onNavigate?.("myjobs")}>View all →</button>
          </div>

          {recentJobs.length === 0 ? (
            <p className="dash-fleet-empty">
              {loadingOngoing || loadingPast ? "Loading your renders…" : "No renders yet — your jobs will show up here."}
            </p>
          ) : (
            <div className="dash-recent-compact">
              {recentJobs.map((job) => {
                const done = job.status === "done";
                const pct = done ? 100 : Math.round(jobProgressPct(job));
                const engine = jobEngineLabel(job);
                const samples = jobSamples(job);
                const sub = engine !== "—" ? (samples ? `${engine} · ${samples} spp` : engine) : "";
                return (
                  <button
                    key={jobKey(job)}
                    type="button"
                    className="dash-rc-row"
                    onClick={() => onNavigate?.("myjobs")}
                  >
                    <span className="dash-recent-thumb">
                      <JobThumbnail job={job} authToken={authToken} backendUrl={backendUrl} />
                    </span>
                    <span className="dash-rc-info">
                      <span className="dash-recent-name">{resolveJobFilename(job)}</span>
                      {sub && <span className="dash-recent-sub">{sub}</span>}
                    </span>
                    <span className="dash-rc-bar">
                      <span
                        style={{
                          width: `${pct}%`,
                          ...(recentBarColor(job.status) ? { background: recentBarColor(job.status) } : {}),
                        }}
                      />
                    </span>
                    <span className="dash-rc-date">{formatDateTimeLabel(job.submitted_at)}</span>
                    <span className={`job-status-badge ${STATUS_TO_BADGE[job.status] || "status-pending"}`}>
                      {STATUS_BADGE_LABELS[job.status] || job.status}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
