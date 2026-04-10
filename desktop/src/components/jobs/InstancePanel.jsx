import { useEffect, useRef, useState } from "react";

const MERGED_STATUS_COLORS = {
  created: "#888", provisioning: "#f5a623", loading: "#f5a623",
  running: "#22c55e", exited: "#f59e0b", stopped: "#f59e0b",
  offline: "#f59e0b", gone: "#6b7280", done: "#22c55e",
  failed: "#ef4444", cancelled: "#6b7280", pending: "#888",
  success: "#22c55e", failure: "#ef4444", timeout: "#ef4444",
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
};

/**
 * Provider strategy must return an object from extractCardData(task, live):
 * { gpuLabel, displayStatus, rendered, total, rangeLabel, elapsedSec, cost,
 *   error, statusMsg, logs, history }
 */

function ActiveCard({ data }) {
  const [showLogs, setShowLogs] = useState(false);
  const logsRef = useRef(null);

  useEffect(() => {
    if (showLogs && logsRef.current) logsRef.current.scrollTop = logsRef.current.scrollHeight;
  }, [data.logs, showLogs]);

  const dot = statusColor(data.displayStatus);
  const pct = data.total ? Math.min(100, Math.round((data.rendered / data.total) * 100)) : null;
  const fillPct = pct ?? 0;

  return (
    <div className="inst-active-card" style={{ "--inst-color": dot }}>
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
        </div>
        <div className="inst-active-stats">
          <StatChip icon={ICON.frames}>{data.rendered}{data.total != null ? ` / ${data.total}` : ""} frames</StatChip>
          {data.rangeLabel && <StatChip icon={ICON.range}>{data.rangeLabel}</StatChip>}
          {data.elapsedSec != null && <StatChip icon={ICON.uptime}>{fmtElapsed(data.elapsedSec)}</StatChip>}
          {data.cost != null && <StatChip icon={ICON.cost}>{data.cost}</StatChip>}
        </div>
        <div className="inst-mini-bar">
          <div className="inst-mini-fill" style={{ width: `${fillPct}%`, background: dot }} />
        </div>

        {data.error && <div className="inst-error">{data.error}</div>}
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

function FinishedRow({ data }) {
  const dot = statusColor(data.displayStatus);
  const [showError, setShowError] = useState(false);

  return (
    <div className="inst-fin-row" style={{ "--inst-color": dot }}>
      <div className="inst-fin-main">
        <span className="inst-fin-dot" style={{ background: dot, boxShadow: `0 0 6px ${dot}55` }} />
        <span className="inst-fin-gpu">{data.gpuLabel}</span>
        <span className="inst-fin-pill" style={{ background: dot + "18", color: dot }}>{data.displayStatus}</span>
        <span className="inst-fin-stat">{data.rendered}{data.total != null ? ` / ${data.total}` : ""} frames</span>
        {data.rangeLabel && <span className="inst-fin-stat inst-fin-range">{data.rangeLabel}</span>}
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
export default function InstancePanel({ tasks, backendUrl, provider }) {
  const [liveMap, setLiveMap] = useState({});

  const filteredTasks = (tasks || []).filter(
    (t) => provider.filterTask(t) && ["pending", "running", "done", "failed", "cancelled"].includes(t.status)
  );
  const activeTasks = filteredTasks.filter((t) => ["pending", "running"].includes(t.status));
  const finishedTasks = filteredTasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status));
  const activeIdsKey = activeTasks.map((t) => t.job_id).join(",");

  useEffect(() => {
    if (!backendUrl || !activeIdsKey) return;
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
  }, [backendUrl, activeIdsKey]);

  if (filteredTasks.length === 0) return null;

  const doneCount = finishedTasks.filter((t) => t.status === "done").length;
  const failedCount = finishedTasks.filter((t) => t.status === "failed").length;

  return (
    <div className="inst-panel">
      <div className="inst-panel-header">
        <div className="inst-panel-title-row">
          <div className="inst-panel-icon">{provider.icon}</div>
          <span className="inst-panel-title">{provider.title}</span>
          <span className="inst-panel-count">{filteredTasks.length}</span>
        </div>
        <div className="inst-panel-chips">
          {activeTasks.length > 0 && <span className="inst-chip inst-chip--active"><span className="inst-chip-dot" style={{ background: "#22c55e" }} />{activeTasks.length} active</span>}
          {doneCount > 0 && <span className="inst-chip inst-chip--done"><span className="inst-chip-dot" style={{ background: "#22c55e" }} />{doneCount} done</span>}
          {failedCount > 0 && <span className="inst-chip inst-chip--failed"><span className="inst-chip-dot" style={{ background: "#f87171" }} />{failedCount} failed</span>}
        </div>
      </div>

      {activeTasks.length > 0 && (
        <div className="inst-active-list">
          {activeTasks.map((task) => (
            <ActiveCard key={task.job_id} data={provider.extractCardData(task, liveMap[task.job_id] || null)} />
          ))}
        </div>
      )}

      {finishedTasks.length > 0 && (
        <div className="inst-fin-list">
          {activeTasks.length > 0 && <div className="inst-fin-divider">Completed</div>}
          {finishedTasks.map((task) => (
            <FinishedRow key={task.job_id} data={provider.extractCardData(task, null)} />
          ))}
        </div>
      )}
    </div>
  );
}

export { gpuShortName, statusColor };
