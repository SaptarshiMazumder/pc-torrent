import { useEffect, useRef, useState } from "react";
import { getVastInstances } from "../../services/api";

const STATUS_COLOR = {
  created: "#888", provisioning: "#f5a623", loading: "#f5a623",
  running: "#22c55e", exited: "#f59e0b", stopped: "#f59e0b",
  offline: "#f59e0b", gone: "#6b7280", done: "#22c55e",
  failed: "#ef4444", cancelled: "#6b7280", pending: "#888",
};

function statusColor(s) { return STATUS_COLOR[s] || "#ef4444"; }

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
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").replace(/^Vast\s+/i, "").trim();
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

/* ── Active card — full detail with progress ring, stats, timeline, logs ── */
function ActiveCard({ task, live }) {
  const [showLogs, setShowLogs] = useState(false);
  const logsRef = useRef(null);

  useEffect(() => {
    if (showLogs && logsRef.current) logsRef.current.scrollTop = logsRef.current.scrollHeight;
  }, [live?.logs, showLogs]);

  const liveVram = live?.gpu_vram_gb ? `${Math.round(live.gpu_vram_gb)}GB` : "";
  const gpuLabel = live?.gpu_model ? `${live.gpu_model} ${liveVram}`.trim() : gpuShortName(task.machine_gpu);
  const rendered = task.rendered_frames ?? 0;
  const total = task.total_frames;
  const actualStatus = live?.actual_status ?? null;
  const dph = live?.dph_total;
  const elapsedSec = live?.elapsed_sec;
  const liveError = live?.error;
  const statusMsg = live?.status_msg || "";
  const logs = live?.logs || "";
  const history = live?.status_history || [];
  const displayStatus = actualStatus || task.status;
  const dot = statusColor(displayStatus);
  const pct = total ? Math.min(100, Math.round((rendered / total) * 100)) : null;
  const rangeLabel = task.frame_start != null && task.frame_end != null ? `${task.frame_start}–${task.frame_end}` : null;
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
          <span className="inst-active-gpu">{gpuLabel}</span>
          <span className="inst-active-pill" style={{ background: dot + "18", color: dot }}>{displayStatus}</span>
        </div>
        <div className="inst-active-stats">
          <StatChip icon={ICON.frames}>{rendered}{total != null ? ` / ${total}` : ""} frames</StatChip>
          {rangeLabel && <StatChip icon={ICON.range}>{rangeLabel}</StatChip>}
          {elapsedSec != null && <StatChip icon={ICON.uptime}>{fmtElapsed(elapsedSec)}</StatChip>}
          {dph != null && <StatChip icon={ICON.cost}>${Number(dph).toFixed(3)}/hr</StatChip>}
        </div>
        <div className="inst-mini-bar">
          <div className="inst-mini-fill" style={{ width: `${fillPct}%`, background: dot }} />
        </div>

        {(liveError || (task.status === "failed" && task.error)) && (
          <div className="inst-error">{liveError || task.error}</div>
        )}
        {statusMsg && !liveError && <div className="inst-status-msg">{statusMsg}</div>}

        {history.length > 1 && (
          <div className="inst-timeline">
            {history.map((h, i) => (
              <span key={i} className="inst-timeline-step">
                {i > 0 && <span className="inst-timeline-arrow">→</span>}
                <span className="inst-timeline-dot" style={{ background: statusColor(h.status) }} />
                <span style={{ color: statusColor(h.status) }}>{h.status}</span>
                <span className="inst-timeline-time">{fmtTime(h.at)}</span>
              </span>
            ))}
          </div>
        )}

        {logs && (
          <div className="inst-logs-section">
            <button className="inst-logs-toggle" onClick={() => setShowLogs((v) => !v)}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
              {showLogs ? "Hide logs" : "Show logs"}
            </button>
            {showLogs && <pre ref={logsRef} className="inst-logs-pre">{logs}</pre>}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Finished row — compact inline for done/failed/cancelled ── */
function FinishedRow({ task }) {
  const rendered = task.rendered_frames ?? 0;
  const total = task.total_frames;
  const dot = statusColor(task.status);
  const rangeLabel = task.frame_start != null && task.frame_end != null ? `${task.frame_start}–${task.frame_end}` : null;
  const hasError = task.status === "failed" && task.error;
  const [showError, setShowError] = useState(false);

  return (
    <div className="inst-fin-row" style={{ "--inst-color": dot }}>
      <div className="inst-fin-main">
        <span className="inst-fin-dot" style={{ background: dot, boxShadow: `0 0 6px ${dot}55` }} />
        <span className="inst-fin-gpu">{gpuShortName(task.machine_gpu)}</span>
        <span className="inst-fin-pill" style={{ background: dot + "18", color: dot }}>{task.status}</span>
        <span className="inst-fin-stat">{rendered}{total != null ? ` / ${total}` : ""} frames</span>
        {rangeLabel && <span className="inst-fin-stat inst-fin-range">{rangeLabel}</span>}
        {hasError && (
          <button className="inst-fin-err-toggle" onClick={() => setShowError((v) => !v)}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4M12 17h.01" /></svg>
          </button>
        )}
      </div>
      {showError && hasError && <div className="inst-error" style={{ marginTop: 6 }}>{task.error}</div>}
    </div>
  );
}

export default function VastInstancePanel({ tasks, backendUrl }) {
  const [liveMap, setLiveMap] = useState({});
  const vastTasks = (tasks || []).filter((t) => {
    const mt = t.machine_type;
    const isVast = mt === "vast_serverless" || (!mt && (t.machine_gpu || "").toLowerCase().includes("vast"));
    return isVast && ["pending", "running", "done", "failed", "cancelled"].includes(t.status);
  });
  const activeTasks = vastTasks.filter((t) => ["pending", "running"].includes(t.status));
  const finishedTasks = vastTasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status));
  const activeIdsKey = activeTasks.map((t) => t.job_id).join(",");

  useEffect(() => {
    if (!backendUrl || !activeIdsKey) return;
    const jobIdSet = new Set(activeTasks.map((t) => t.job_id));
    let cancelled = false;
    async function poll() {
      try {
        const all = await getVastInstances(backendUrl);
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

  if (vastTasks.length === 0) return null;

  const doneCount = finishedTasks.filter((t) => t.status === "done").length;
  const failedCount = finishedTasks.filter((t) => t.status === "failed").length;

  return (
    <div className="inst-panel">
      <div className="inst-panel-header">
        <div className="inst-panel-title-row">
          <div className="inst-panel-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round"><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 14h.01M10 14h.01M14 14h.01" /></svg>
          </div>
          <span className="inst-panel-title">GPU Instances</span>
          <span className="inst-panel-count">{vastTasks.length}</span>
        </div>
        <div className="inst-panel-chips">
          {activeTasks.length > 0 && <span className="inst-chip inst-chip--active"><span className="inst-chip-dot" style={{ background: "#22c55e" }} />{activeTasks.length} active</span>}
          {doneCount > 0 && <span className="inst-chip inst-chip--done"><span className="inst-chip-dot" style={{ background: "#22c55e" }} />{doneCount} done</span>}
          {failedCount > 0 && <span className="inst-chip inst-chip--failed"><span className="inst-chip-dot" style={{ background: "#f87171" }} />{failedCount} failed</span>}
        </div>
      </div>

      {/* Active / in-progress instances — full cards */}
      {activeTasks.length > 0 && (
        <div className="inst-active-list">
          {activeTasks.map((task) => (
            <ActiveCard key={task.job_id} task={task} live={liveMap[task.job_id] || null} />
          ))}
        </div>
      )}

      {/* Finished instances — compact rows */}
      {finishedTasks.length > 0 && (
        <div className="inst-fin-list">
          {activeTasks.length > 0 && <div className="inst-fin-divider">Completed</div>}
          {finishedTasks.map((task) => (
            <FinishedRow key={task.job_id} task={task} />
          ))}
        </div>
      )}
    </div>
  );
}
