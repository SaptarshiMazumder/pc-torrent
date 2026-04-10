import { useEffect, useRef, useState } from "react";
import { getModalInstances } from "../lib/api";

const STATUS_COLOR = {
  pending: "#888",
  running: "#22c55e",
  done: "#22c55e",
  failed: "#ef4444",
  cancelled: "#6b7280",
  gone: "#6b7280",
  success: "#22c55e",
  failure: "#ef4444",
  timeout: "#ef4444",
  terminated: "#ef4444",
  initializing: "#a78bfa",
  loading: "#a78bfa",
  queued: "#a78bfa",
};

function statusColor(status) {
  if (!status) return "#888";
  const key = String(status).toLowerCase();
  return STATUS_COLOR[key] || "#ef4444";
}

function fmtElapsed(sec) {
  if (sec == null) return "-";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtTime(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleTimeString();
}

function InstanceCard({ task, live }) {
  const [showLogs, setShowLogs] = useState(false);
  const logsRef = useRef(null);

  useEffect(() => {
    if (showLogs && logsRef.current) {
      logsRef.current.scrollTop = logsRef.current.scrollHeight;
    }
  }, [showLogs, live?.logs]);

  const providerStatus = live?.provider_status || null;
  const jobStatus = task.status;
  const displayStatus = providerStatus || jobStatus;
  const dot = statusColor(displayStatus);

  const providerJobId = live?.provider_job_id || null;
  const framesDone = live?.rendered_frames ?? task.rendered_frames ?? 0;
  const framesTotal = live?.total_frames ?? task.total_frames ?? null;
  const outputCount = live?.output_count ?? 0;
  const frameStart = live?.frame_start ?? task.frame_start;
  const frameEnd = live?.frame_end ?? task.frame_end;
  const elapsedSec = live?.elapsed_sec ?? null;
  const lastPoll = live?.last_poll_at || null;
  const liveError = live?.error || "";
  const liveAction = live?.monitor_action || "";
  const logs = live?.logs || "";
  const history = Array.isArray(live?.status_history) ? live.status_history : [];

  const gpuLabel =
    task.machine_gpu ||
    (live?.gpu_type ? `Modal ${String(live.gpu_type).toUpperCase()}` : "Modal GPU");

  const framesLabel =
    framesTotal != null ? `${framesDone} / ${framesTotal}` : `${framesDone} rendered`;

  return (
    <div
      style={{
        background: "var(--bg-secondary, #1a1a1a)",
        border: `1px solid ${dot}44`,
        borderLeft: `3px solid ${dot}`,
        borderRadius: 8,
        padding: "10px 14px",
        fontSize: 12,
        fontFamily: "monospace",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
          marginBottom: 6,
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: dot,
            display: "inline-block",
            flexShrink: 0,
            boxShadow: displayStatus === "running" ? `0 0 6px ${dot}` : "none",
          }}
        />
        <strong style={{ color: "#e5e7eb" }}>{gpuLabel}</strong>
        {providerJobId && <span style={{ color: "#6b7280" }}>{providerJobId}</span>}
        <span
          style={{
            padding: "1px 7px",
            borderRadius: 4,
            fontSize: 11,
            background: dot + "22",
            color: dot,
            fontWeight: 600,
          }}
        >
          {providerStatus ? `${providerStatus} / ${jobStatus}` : jobStatus}
        </span>
      </div>

      <div style={{ display: "flex", gap: 16, color: "#9ca3af", flexWrap: "wrap", marginBottom: 4 }}>
        <span>
          Frames: <span style={{ color: "#d1d5db" }}>{framesLabel}</span>
        </span>
        {frameStart != null && frameEnd != null && (
          <span style={{ color: "#6b7280" }}>
            range {frameStart}-{frameEnd}
          </span>
        )}
        <span>
          Outputs: <span style={{ color: "#d1d5db" }}>{outputCount}</span>
        </span>
        {elapsedSec != null && (
          <span>
            Up: <span style={{ color: "#d1d5db" }}>{fmtElapsed(elapsedSec)}</span>
          </span>
        )}
        {lastPoll && (
          <span style={{ marginLeft: "auto", color: "#4b5563" }}>
            polled {fmtTime(lastPoll)}
          </span>
        )}
        {!live && !["done", "failed", "cancelled"].includes(jobStatus) && (
          <span style={{ marginLeft: "auto", color: "#4b5563", fontStyle: "italic" }}>
            live data pending...
          </span>
        )}
      </div>

      {liveAction && (
        <div style={{ color: "#9ca3af", marginTop: 4, wordBreak: "break-word", fontSize: 11 }}>
          {liveAction}
        </div>
      )}

      {(liveError || (task.status === "failed" && task.error)) && (
        <div style={{ color: "#f87171", marginTop: 4, wordBreak: "break-word" }}>
          {liveError || task.error}
        </div>
      )}

      {history.length > 1 && (
        <div style={{ color: "#6b7280", marginTop: 4, fontSize: 11 }}>
          {history.map((h, i) => (
            <span key={i}>
              {i > 0 && <span style={{ color: "#374151" }}> -&gt; </span>}
              <span style={{ color: statusColor(h.status) }}>{h.status}</span>
              <span style={{ color: "#374151" }}> @{fmtTime(h.at)}</span>
            </span>
          ))}
        </div>
      )}

      {logs && (
        <div style={{ marginTop: 6 }}>
          <button
            onClick={() => setShowLogs((v) => !v)}
            style={{
              background: "none",
              border: "1px solid #374151",
              color: "#9ca3af",
              borderRadius: 4,
              padding: "2px 8px",
              cursor: "pointer",
              fontSize: 11,
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

export default function ModalInstancePanel({ tasks, backendUrl }) {
  const [liveMap, setLiveMap] = useState({});

  const modalTasks = (tasks || []).filter((task) => {
    const machineType = task.machine_type;
    const isModal =
      machineType === "modal_serverless" ||
      (!machineType && (task.machine_gpu || "").toLowerCase().includes("modal"));
    if (!isModal) return false;
    return ["pending", "running", "done", "failed", "cancelled"].includes(task.status);
  });

  const activeTasks = modalTasks.filter((task) => ["pending", "running"].includes(task.status));
  const activeIdsKey = activeTasks.map((task) => task.job_id).join(",");

  useEffect(() => {
    if (!backendUrl || !activeIdsKey) return;
    const jobIdSet = new Set(activeTasks.map((task) => task.job_id));
    let cancelled = false;

    async function poll() {
      try {
        const all = await getModalInstances(backendUrl);
        if (!cancelled && Array.isArray(all)) {
          const map = {};
          for (const entry of all) {
            if (jobIdSet.has(entry.job_id)) map[entry.job_id] = entry;
          }
          setLiveMap(map);
        }
      } catch {
        // endpoint may be absent on older backend builds
      }
    }

    void poll();
    const id = setInterval(poll, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [backendUrl, activeIdsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  if (modalTasks.length === 0) return null;

  return (
    <div style={{ marginTop: 10 }}>
      <div
        style={{
          fontSize: 11,
          color: "#6b7280",
          marginBottom: 6,
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span>Modal instances ({modalTasks.length})</span>
        {Object.keys(liveMap).length > 0 && (
          <span style={{ color: "#22c55e", fontWeight: 600 }}>live</span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {modalTasks.map((task) => (
          <InstanceCard key={task.job_id} task={task} live={liveMap[task.job_id] || null} />
        ))}
      </div>
    </div>
  );
}
