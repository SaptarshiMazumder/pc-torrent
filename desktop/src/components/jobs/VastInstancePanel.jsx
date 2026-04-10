import { useEffect, useRef, useState } from "react";
import { getVastInstances } from "../../services/api";

const STATUS_COLOR = {
  created:      "#888",
  provisioning: "#a78bfa",
  loading:      "#a78bfa",
  running:      "#22c55e",
  exited:       "#f59e0b",
  stopped:      "#f59e0b",
  offline:      "#f59e0b",
  gone:         "#6b7280",
  done:         "#22c55e",
  failed:       "#ef4444",
  cancelled:    "#6b7280",
  pending:      "#888",
};

function statusColor(s) {
  return STATUS_COLOR[s] || "#ef4444";
}

function fmtElapsed(sec) {
  if (sec == null) return "—";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString();
}

function InstanceCard({ task, live }) {
  const [showLogs, setShowLogs] = useState(false);
  const logsRef = useRef(null);

  useEffect(() => {
    if (showLogs && logsRef.current) {
      logsRef.current.scrollTop = logsRef.current.scrollHeight;
    }
  }, [live?.logs, showLogs]);

  // What we always know from the task (DB data via group status API)
  const liveVram = live?.gpu_vram_gb ? `${Math.round(live.gpu_vram_gb)}GB` : "";
  const gpuLabel = live?.gpu_model
    ? `Vast ${live.gpu_model} ${liveVram}`.trim()
    : (task.machine_gpu || "Vast GPU");
  const jobStatus  = task.status;
  const frameStart = task.frame_start;
  const frameEnd   = task.frame_end;
  const rendered   = task.rendered_frames ?? 0;
  const total      = task.total_frames;
  const instanceId = task.runpod_job_id; // Vast stores instance_id here

  // Live data from the in-memory registry (may be absent for older jobs)
  const actualStatus  = live?.actual_status ?? null;
  const dph           = live?.dph_total;
  const elapsedSec    = live?.elapsed_sec;
  const lastPoll      = live?.last_poll_at;
  const liveError     = live?.error;
  const statusMsg     = live?.status_msg || "";
  const logs          = live?.logs || "";
  const history       = live?.status_history || [];

  // Derive a display status: prefer actual Vast status, fall back to job status
  const displayStatus = actualStatus || jobStatus;
  const dot = statusColor(displayStatus);

  const framesLabel = total != null
    ? `${rendered} / ${total}`
    : `${rendered} rendered`;

  return (
    <div style={{
      background: "var(--bg-secondary, #1a1a1a)",
      border: `1px solid ${dot}44`,
      borderLeft: `3px solid ${dot}`,
      borderRadius: 8,
      padding: "10px 14px",
      fontSize: 12,
      fontFamily: "monospace",
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
        <span style={{
          width: 8, height: 8, borderRadius: "50%",
          background: dot, display: "inline-block", flexShrink: 0,
          boxShadow: displayStatus === "running" ? `0 0 6px ${dot}` : "none",
        }} />
        <strong style={{ color: "#e5e7eb" }}>{gpuLabel}</strong>
        {instanceId && <span style={{ color: "#6b7280" }}>#{instanceId}</span>}
        <span style={{
          padding: "1px 7px", borderRadius: 4, fontSize: 11,
          background: dot + "22", color: dot, fontWeight: 600,
        }}>
          {actualStatus ? `${actualStatus} / ${jobStatus}` : jobStatus}
        </span>
        {dph != null && (
          <span style={{ color: "#6b7280", marginLeft: "auto" }}>
            ${Number(dph).toFixed(3)}/hr
          </span>
        )}
      </div>

      {/* Stats */}
      <div style={{ display: "flex", gap: 16, color: "#9ca3af", flexWrap: "wrap", marginBottom: 4 }}>
        <span>Frames: <span style={{ color: "#d1d5db" }}>{framesLabel}</span></span>
        {frameStart != null && frameEnd != null && (
          <span style={{ color: "#6b7280" }}>range {frameStart}–{frameEnd}</span>
        )}
        {elapsedSec != null && (
          <span>Up: <span style={{ color: "#d1d5db" }}>{fmtElapsed(elapsedSec)}</span></span>
        )}
        {lastPoll && (
          <span style={{ marginLeft: "auto", color: "#4b5563" }}>polled {fmtTime(lastPoll)}</span>
        )}
        {!live && !["done", "failed", "cancelled"].includes(jobStatus) && (
          <span style={{ marginLeft: "auto", color: "#4b5563", fontStyle: "italic" }}>
            live data pending…
          </span>
        )}
      </div>

      {/* Error */}
      {(liveError || (task.status === "failed" && task.error)) && (
        <div style={{ color: "#f87171", marginTop: 4, wordBreak: "break-word" }}>
          {liveError || task.error}
        </div>
      )}
      {/* Vast status_msg — shows container startup errors before they trigger failover */}
      {statusMsg && !liveError && (
        <div style={{ color: "#9ca3af", marginTop: 4, wordBreak: "break-word", fontSize: 11 }}>
          {statusMsg}
        </div>
      )}

      {/* Status history */}
      {history.length > 1 && (
        <div style={{ color: "#6b7280", marginTop: 4, fontSize: 11 }}>
          {history.map((h, i) => (
            <span key={i}>
              {i > 0 && <span style={{ color: "#374151" }}> → </span>}
              <span style={{ color: statusColor(h.status) }}>{h.status}</span>
              <span style={{ color: "#374151" }}> @{fmtTime(h.at)}</span>
            </span>
          ))}
        </div>
      )}

      {/* Logs */}
      {logs && (
        <div style={{ marginTop: 6 }}>
          <button
            onClick={() => setShowLogs((v) => !v)}
            style={{
              background: "none", border: "1px solid #374151", color: "#9ca3af",
              borderRadius: 4, padding: "2px 8px", cursor: "pointer", fontSize: 11,
            }}
          >
            {showLogs ? "Hide logs" : "Show logs"}
          </button>
          {showLogs && (
            <pre
              ref={logsRef}
              style={{
                marginTop: 6,
                background: "#060606",
                border: "1px solid #1f2937",
                borderRadius: 4,
                padding: "8px 10px",
                fontSize: 11,
                maxHeight: 300,
                overflowY: "auto",
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                color: "#d1d5db",
              }}
            >
              {logs}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Shows a live status card for every Vast task in a render group.
 * Works from task data immediately (no registry needed).
 * Enriches with real-time Vast instance data from /vast/instances
 * once the server is running the new polling code.
 *
 * Props:
 *   tasks      - array of task objects from the render group (all, not just vast)
 *   backendUrl - backend base URL
 */
export default function VastInstancePanel({ tasks, backendUrl }) {
  const [liveMap, setLiveMap] = useState({}); // job_id -> registry entry

  // Filter to vast tasks that are active or recently terminal
  const vastTasks = (tasks || []).filter(
    (t) => {
      const mt = t.machine_type;
      const isVast = mt === "vast_serverless"
        || (!mt && (t.machine_gpu || "").toLowerCase().includes("vast"));
      if (!isVast) return false;
      return ["pending", "running", "done", "failed", "cancelled"].includes(t.status);
    }
  );

  const activeTasks = vastTasks.filter(
    (t) => ["pending", "running"].includes(t.status)
  );
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
          for (const entry of all) {
            if (jobIdSet.has(entry.job_id)) map[entry.job_id] = entry;
          }
          setLiveMap(map);
        }
      } catch {
        // silently ignore — endpoint may not exist on older server
      }
    }

    void poll();
    const id = setInterval(poll, 5_000);
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendUrl, activeIdsKey]);

  if (vastTasks.length === 0) return null;

  return (
    <div style={{ marginTop: 10 }}>
      <div style={{
        fontSize: 11, color: "#6b7280", marginBottom: 6,
        textTransform: "uppercase", letterSpacing: "0.05em",
        display: "flex", alignItems: "center", gap: 8,
      }}>
        <span>Vast.ai instances ({vastTasks.length})</span>
        {Object.keys(liveMap).length > 0 && (
          <span style={{ color: "#22c55e", fontWeight: 600 }}>● live</span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {vastTasks.map((task) => (
          <InstanceCard
            key={task.job_id}
            task={task}
            live={liveMap[task.job_id] || null}
          />
        ))}
      </div>
    </div>
  );
}
