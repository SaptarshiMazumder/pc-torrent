import { useEffect, useRef, useState } from "react";
import { cancelJob, getAllowedStallTimes } from "../../services/api";
import { formatCredits } from "../../utils/creditsFormat";
import AllowedStallTimesOverlay from "./AllowedStallTimesOverlay";

function formatActualCost(task) {
  // ``actual_cost_credits`` is computed server-side on every read.
  // Running tasks substitute ``now`` in the formula; terminal tasks
  // have frozen values.  Server applies the priority multiplier and
  // USD->credits conversion at the wire boundary, so we just display.
  if (typeof task?.actual_cost_credits !== "number") return null;
  const label = (task.status === "running" || task.status === "uploading")
    ? "live"
    : "actual";
  return `${formatCredits(task.actual_cost_credits)} tokens ${label}`;
}

const MERGED_STATUS_COLORS = {
  created: "#9a8d7b", provisioning: "#f5a623", loading: "#f5a623",
  running: "#12a150", exited: "#f59e0b", stopped: "#f59e0b",
  offline: "#f59e0b", gone: "#9a8d7b", done: "#12a150",
  failed: "#ef4444", cancelled: "#9a8d7b", pending: "#9a8d7b",
  success: "#12a150", failure: "#ef4444", timeout: "#ef4444",
  terminated: "#ef4444", initializing: "#f5a623", queued: "#f5a623",
};

function statusColor(s) {
  return MERGED_STATUS_COLORS[String(s || "").toLowerCase()] || "#ef4444";
}

function fmtElapsed(sec) {
  if (sec == null) return null;
  const m = Math.floor(sec / 60);
  const s2 = sec % 60;
  return m > 0 ? `${m}m ${s2}s` : `${s2}s`;
}

function fmtTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString();
}

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").replace(/^Vast\s+/i, "").replace(/^Modal\s+/i, "").trim();
}

function CircleProgress({ pct, color, size = 48, stroke = 3.5 }) {
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;
  return (
    <svg width={size} height={size} className="inst-ring">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none"
        stroke="rgba(255,255,255,0.04)" strokeWidth={stroke} />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none"
        stroke={color} strokeWidth={stroke} strokeLinecap="round"
        strokeDasharray={circ} strokeDashoffset={offset}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dashoffset 0.4s ease", filter: `drop-shadow(0 0 6px ${color}44)` }}
      />
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central"
        fill="#eaeaf1" fontSize="11" fontWeight="700" fontFamily="Inter, sans-serif">
        {pct}%
      </text>
    </svg>
  );
}

function StatChip({ icon, children }) {
  return (
    <span className="inst-stat-chip">
      {icon}
      <span>{children}</span>
    </span>
  );
}

const ICON = {
  frames: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>,
  range: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17 8l4 4-4 4M7 16l-4-4 4-4M14 4l-4 16" /></svg>,
  uptime: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" /></svg>,
  cost: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>,
  stall: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4M12 17h.01" /></svg>,
};

// Allowed stall-time chip math used to live here as hardcoded constants
// mirroring serverV2/config.json "stall.loading_*".  After the
// allowed-stall-times refactor, deadlines are stamped on the job row at
// dispatch and exposed via GET /jobs/{id}/allowed-stall-times.  The
// per-card overlay (AllowedStallTimesOverlay) consumes that endpoint
// directly; no client-side clamp math.

/**
 * Provider strategy must return an object from extractCardData(task, live):
 * { gpuLabel, displayStatus, rendered, total, rangeLabel, elapsedSec, cost,
 *   error, statusMsg, logs, history }
 */

function ActiveCard({ data, jobId, backendUrl, onCancel }) {
  const [showLogs, setShowLogs] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const logsRef = useRef(null);

  // Allowed-stall-times: fetch once on mount, cache forever for this card.
  // The deadline values are immutable per chunk-attempt, so a single
  // fetch is sufficient -- no polling.  Server returns 404 for legacy
  // rows that pre-date the column; the api wrapper maps that to null.
  const [stallTimes, setStallTimes] = useState(null);
  const [stallTimesLoading, setStallTimesLoading] = useState(true);
  const [stallTimesOpen, setStallTimesOpen] = useState(false);
  const clockBtnRef = useRef(null);
  const cardRef = useRef(null);

  useEffect(() => {
    if (!backendUrl || !jobId) {
      setStallTimesLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const result = await getAllowedStallTimes(backendUrl, jobId);
        if (!cancelled) setStallTimes(result);
      } catch {
        // Network error -- treat as null; overlay shows "no data" message.
      } finally {
        if (!cancelled) setStallTimesLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [backendUrl, jobId]);

  useEffect(() => {
    if (showLogs && logsRef.current) logsRef.current.scrollTop = logsRef.current.scrollHeight;
  }, [data.logs, showLogs]);

  async function handleCancel() {
    if (cancelling || !jobId || !backendUrl) return;
    setCancelling(true);
    setCancelError("");
    try {
      await cancelJob(backendUrl, jobId);
      // Server returned 202; the cancel chain runs in a background task.
      // Refresh so the UI moves this card from active to finished as
      // soon as the server's row flips to 'cancelled'.
      if (onCancel) onCancel();
    } catch (e) {
      setCancelling(false);
      setCancelError(e?.message || "Cancel failed");
    }
  }

  const dot = statusColor(data.displayStatus);
  const pct = data.total ? Math.min(100, Math.round((data.rendered / data.total) * 100)) : null;
  const fillPct = pct ?? 0;
  const canCancel = Boolean(jobId && backendUrl && onCancel);

  return (
    <div
      ref={cardRef}
      className="inst-active-card"
      style={{
        "--inst-color": dot,
        ...(stallTimesOpen
          ? { borderBottomLeftRadius: 0, borderBottomRightRadius: 0, borderBottom: "none" }
          : null),
      }}
    >
      <div className="inst-active-left">
        {pct != null ? <CircleProgress pct={pct} color={dot} /> : (
          <div className="inst-active-avatar" style={{ borderColor: dot }}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={dot} strokeWidth="1.8"><rect x="4" y="4" width="16" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></svg>
          </div>
        )}
      </div>
      <div className="inst-active-right">
        <div className="inst-active-header">
          <span className="inst-active-gpu">{data.gpuLabel}</span>
          <span className="inst-active-pill" style={{ background: dot + "18", color: dot }}>{data.displayStatus}</span>
          <span style={{ marginLeft: "auto", display: "inline-flex" }}>
            <button
              ref={clockBtnRef}
              type="button"
              onClick={() => setStallTimesOpen((v) => !v)}
              title="Allowed stall times"
              aria-label="Show allowed stall times"
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: 22,
                height: 22,
                padding: 0,
                borderRadius: "50%",
                border: "1px solid rgba(255,255,255,0.12)",
                background: stallTimesOpen ? "rgba(255,255,255,0.08)" : "transparent",
                color: stallTimesOpen ? "#eaeaf1" : "#9090a0",
                cursor: "pointer",
                transition: "background 0.15s ease, color 0.15s ease",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(255,255,255,0.05)"; e.currentTarget.style.color = "#eaeaf1"; }}
              onMouseLeave={(e) => {
                if (!stallTimesOpen) {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "#9090a0";
                }
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" />
                <path d="M12 6v6l4 2" />
              </svg>
            </button>
            {stallTimesOpen && (
              <AllowedStallTimesOverlay
                anchorRef={cardRef}
                ignoreClickRef={clockBtnRef}
                stallTimes={stallTimes}
                isLoading={stallTimesLoading}
                onClose={() => setStallTimesOpen(false)}
              />
            )}
          </span>
          {canCancel && (
            <button
              type="button"
              className="inst-active-cancel-btn"
              onClick={handleCancel}
              disabled={cancelling}
              title={cancelling ? "Cancelling…" : "Cancel this chunk"}
              aria-label="Cancel this chunk"
              style={{
                marginLeft: 6,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: 22,
                height: 22,
                padding: 0,
                borderRadius: "50%",
                border: "1px solid rgba(255,255,255,0.12)",
                background: cancelling ? "rgba(239,68,68,0.25)" : "transparent",
                color: "#ef4444",
                cursor: cancelling ? "default" : "pointer",
                opacity: cancelling ? 0.6 : 1,
                transition: "background 0.15s ease, border-color 0.15s ease",
              }}
              onMouseEnter={(e) => { if (!cancelling) e.currentTarget.style.background = "rgba(239,68,68,0.18)"; }}
              onMouseLeave={(e) => { if (!cancelling) e.currentTarget.style.background = "transparent"; }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <line x1="6" y1="6" x2="18" y2="18" />
                <line x1="18" y1="6" x2="6" y2="18" />
              </svg>
            </button>
          )}
        </div>
        <div className="inst-active-stats">
          <StatChip icon={ICON.frames}>{data.rendered}{data.total != null ? ` / ${data.total}` : ""} frames</StatChip>
          {data.rangeLabel && <StatChip icon={ICON.range}>{data.rangeLabel}</StatChip>}
          {data.elapsedSec != null && <StatChip icon={ICON.uptime}>{fmtElapsed(data.elapsedSec)}</StatChip>}
          {data.cost != null && <StatChip icon={ICON.cost}>{data.cost}</StatChip>}
          {data.actualCost != null && <StatChip icon={ICON.cost}>{data.actualCost}</StatChip>}
        </div>
        <div className="inst-mini-bar">
          <div className="inst-mini-fill" style={{ width: `${fillPct}%`, background: dot }} />
        </div>

        {data.error && <div className="inst-error">{data.error}</div>}
        {cancelError && <div className="inst-error">{cancelError}</div>}
        {data.statusMsg && !data.error && <div className="inst-status-msg">{data.statusMsg}</div>}

        {data.history.length > 1 && (
          <div className="inst-timeline">
            {data.history.map((h, i) => (
              <span key={i} className="inst-timeline-step">
                {i > 0 && <span className="inst-timeline-arrow">→</span>}
                <span className="inst-timeline-dot" style={{ background: statusColor(h.status) }} />
                <span style={{ color: statusColor(h.status) }}>{h.status}</span>
                <span className="inst-timeline-time">{fmtTime(h.at)}</span>
              </span>
            ))}
          </div>
        )}

        {data.logs && (
          <div className="inst-logs-section">
            <button className="inst-logs-toggle" onClick={() => setShowLogs((v) => !v)}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
              {showLogs ? "Hide logs" : "Show logs"}
            </button>
            {showLogs && <pre ref={logsRef} className="inst-logs-pre">{data.logs}</pre>}
          </div>
        )}
      </div>
    </div>
  );
}

// Maps backend stall-rule names (parsed from job.error in the
// serializer) to compact UI labels.  Add new entries here when new
// rules are added to PreRenderStallDetector.
const STALL_RULE_LABELS = {
  loading_stall: "loading stall",
  bytes_stall: "bytes stall",
  download_ceiling: "download ceiling",
  cpu_stall: "cpu stall",
  hard_ceiling: "hard ceiling",
};

function FinishedRow({ data }) {
  const dot = statusColor(data.displayStatus);
  const [showError, setShowError] = useState(false);
  const stallLabel = data.stallRule ? (STALL_RULE_LABELS[data.stallRule] || data.stallRule) : null;

  return (
    <div className="inst-fin-row" style={{ "--inst-color": dot }}>
      <div className="inst-fin-main">
        <span className="inst-fin-dot" style={{ background: dot, boxShadow: `0 0 6px ${dot}55` }} />
        <span className="inst-fin-gpu">{data.gpuLabel}</span>
        <span className="inst-fin-pill" style={{ background: dot + "18", color: dot }}>{data.displayStatus}</span>
        {stallLabel && (
          <span
            className="inst-fin-pill"
            style={{ background: "#f59e0b18", color: "#f59e0b", fontWeight: 600 }}
            title={`Stall rule: ${data.stallRule}`}
          >
            {stallLabel}
          </span>
        )}
        <span className="inst-fin-stat">{data.rendered}{data.total != null ? ` / ${data.total}` : ""} frames</span>
        {data.rangeLabel && <span className="inst-fin-stat inst-fin-range">{data.rangeLabel}</span>}
        {data.actualCost != null && (
          <span className="inst-fin-stat" title="Actual cost (price_per_hour × elapsed)">{data.actualCost}</span>
        )}
        {data.error && (
          <button className="inst-fin-err-toggle" onClick={() => setShowError((v) => !v)}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4M12 17h.01" /></svg>
          </button>
        )}
      </div>
      {showError && data.error && <div className="inst-error" style={{ marginTop: 6 }}>{data.error}</div>}
    </div>
  );
}

/**
 * Generic instance panel driven by a provider strategy.
 *
 * @param {Object} provider
 * @param {string} provider.title
 * @param {ReactNode} provider.icon
 * @param {(task) => boolean} provider.filterTask
 * @param {(backendUrl) => Promise<Array>} provider.fetchInstances
 * @param {(task, live) => CardData} provider.extractCardData
 */
export default function InstancePanel({ tasks, backendUrl, provider, onRefresh, mode = "both" }) {
  const [liveMap, setLiveMap] = useState({});
  // Cost values come from the polled task DTOs (server-computed live
  // for running tasks).  No local 1Hz tick.

  const filteredTasks = (tasks || []).filter(
    (t) => provider.filterTask(t) && ["pending", "running", "done", "failed", "cancelled"].includes(t.status)
  );
  const activeTasks = filteredTasks.filter((t) => ["pending", "running"].includes(t.status));
  const finishedTasks = filteredTasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status));
  const activeIdsKey = activeTasks.map((t) => t.job_id).join(",");

  const showActive = mode !== "finished";
  const showFinished = mode !== "active";
  const shouldPoll = showActive && !!activeIdsKey;

  useEffect(() => {
    if (!backendUrl || !shouldPoll) return;
    const jobIdSet = new Set(activeTasks.map((t) => t.job_id));
    let cancelled = false;
    async function poll() {
      try {
        const all = await provider.fetchInstances(backendUrl);
        if (!cancelled && Array.isArray(all)) {
          const map = {};
          for (const entry of all) { if (jobIdSet.has(entry.job_id)) map[entry.job_id] = entry; }
          setLiveMap(map);
        }
      } catch {}
    }
    void poll();
    const id = setInterval(poll, 5_000);
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendUrl, activeIdsKey, shouldPoll]);

  const visibleCount = (showActive ? activeTasks.length : 0) + (showFinished ? finishedTasks.length : 0);
  if (visibleCount === 0) return null;

  const doneCount = finishedTasks.filter((t) => t.status === "done").length;
  const failedCount = finishedTasks.filter((t) => t.status === "failed").length;

  return (
    <div className="inst-panel">
      <div className="inst-panel-header">
        <div className="inst-panel-title-row">
          <div className="inst-panel-icon">{provider.icon}</div>
          <span className="inst-panel-title">{provider.title}</span>
          <span className="inst-panel-count">{visibleCount}</span>
        </div>
        <div className="inst-panel-chips">
          {showActive && activeTasks.length > 0 && <span className="inst-chip inst-chip--active"><span className="inst-chip-dot" style={{ background: "#22c55e" }} />{activeTasks.length} active</span>}
          {showFinished && doneCount > 0 && <span className="inst-chip inst-chip--done"><span className="inst-chip-dot" style={{ background: "#22c55e" }} />{doneCount} done</span>}
          {showFinished && failedCount > 0 && <span className="inst-chip inst-chip--failed"><span className="inst-chip-dot" style={{ background: "#f87171" }} />{failedCount} failed</span>}
        </div>
      </div>

      {showActive && activeTasks.length > 0 && (
        <div className="inst-active-list">
          {activeTasks.map((task) => {
            const data = provider.extractCardData(task, liveMap[task.job_id] || null);
            data.actualCost = formatActualCost(task);
            return (
              <ActiveCard
                key={task.job_id}
                data={data}
                jobId={task.job_id}
                backendUrl={backendUrl}
                onCancel={onRefresh}
              />
            );
          })}
        </div>
      )}

      {showFinished && finishedTasks.length > 0 && (
        <div className="inst-fin-list">
          {showActive && activeTasks.length > 0 && <div className="inst-fin-divider">Completed</div>}
          {finishedTasks.map((task) => {
            const data = provider.extractCardData(task, null);
            data.actualCost = formatActualCost(task);
            return <FinishedRow key={task.job_id} data={data} />;
          })}
        </div>
      )}
    </div>
  );
}

export { gpuShortName, statusColor };
