import { useState, useEffect, useRef } from "react";
import {
  getMachines,
  submitDistributedJob,
  confirmDistributedJob,
  getRenderGroup,
  renderGroupDownloadUrl,
} from "./api";
import "./App.css";

const SEGMENT_COLORS = [
  "#6c63ff", "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
];

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").trim();
}

// -----------------------------------------------
// Segmented Progress Bar
// -----------------------------------------------
function SegmentedProgressBar({ tasks, totalFrames }) {
  if (!tasks || tasks.length === 0 || !totalFrames) return null;
  return (
    <div className="seg-wrap">
      <div className="seg-track">
        {tasks.map((task, i) => {
          const segWidth = totalFrames > 0 ? (task.total_frames / totalFrames) * 100 : 0;
          const fillPct =
            task.status === "done"
              ? 100
              : typeof task.progress_pct === "number"
              ? Math.max(0, Math.min(100, task.progress_pct))
              : 0;
          const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
          const isFailed = task.status === "failed";
          return (
            <div
              key={task.job_id}
              className={`seg-segment ${isFailed ? "seg-failed" : ""}`}
              style={{ width: `${Math.max(segWidth, 1)}%` }}
              title={`${gpuShortName(task.machine_gpu)}: frames ${task.frame_start}-${task.frame_end}`}
            >
              <div
                className="seg-fill"
                style={{
                  width: `${fillPct}%`,
                  background: isFailed
                    ? "repeating-linear-gradient(45deg,#ef4444 0,#ef4444 4px,#7f1d1d 4px,#7f1d1d 8px)"
                    : task.status === "pending"
                    ? "transparent"
                    : color,
                }}
              />
            </div>
          );
        })}
      </div>
      <div className="seg-labels">
        {tasks.map((task, i) => {
          const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
          const pct =
            task.status === "done" ? 100
            : typeof task.progress_pct === "number"
            ? Math.round(task.progress_pct)
            : 0;
          return (
            <div key={task.job_id} className="seg-label">
              <span className="seg-dot" style={{ background: task.status === "failed" ? "#ef4444" : color }} />
              <span className="seg-gpu">{gpuShortName(task.machine_gpu)}</span>
              <span className="seg-range">{task.frame_start}&ndash;{task.frame_end}</span>
              <span className={`seg-pct ${task.status === "done" ? "pct-done" : task.status === "failed" ? "pct-failed" : ""}`}>
                {task.status === "failed" ? "Failed" : task.status === "pending" ? "Waiting" : `${pct}%`}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// -----------------------------------------------
// Page: Browse Machines (multi-select)
// -----------------------------------------------
function MachinesPage({ onContinue }) {
  const [machines, setMachines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState([]);

  const load = () => {
    setLoading(true);
    getMachines()
      .then(setMachines)
      .catch(() => setError("Cannot reach backend"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const toggle = (m) => {
    setSelected((prev) => {
      const exists = prev.find((s) => s.id === m.id);
      return exists ? prev.filter((s) => s.id !== m.id) : [...prev, m];
    });
  };

  const powerScore = (m) =>
    (m.gpu_vram_gb || 0) * 4 + (m.cpu_cores || 0) + (m.ram_gb || 0) * 0.3;
  const totalPower = selected.reduce((s, m) => s + powerScore(m), 0);

  if (loading) return <p className="status">Loading machines...</p>;
  if (error) return <p className="error">{error}</p>;

  return (
    <div>
      <div className="page-header">
        <h2>Available Machines</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button className="btn-secondary" onClick={load}>Refresh</button>
          {selected.length > 0 && (
            <button className="btn-primary" onClick={() => onContinue(selected)}>
              Rent {selected.length} machine{selected.length > 1 ? "s" : ""}
            </button>
          )}
        </div>
      </div>

      {selected.length > 0 && (
        <div className="selection-hint">
          <strong>{selected.length} selected</strong> — frames will be split proportionally by GPU power.
        </div>
      )}

      {machines.length === 0 ? (
        <p className="status">No machines available right now.</p>
      ) : (
        <table className="machines-table">
          <thead>
            <tr>
              <th></th>
              <th>GPU</th>
              <th>VRAM</th>
              <th>CPU Cores</th>
              <th>RAM</th>
              {selected.length > 0 && <th>Share</th>}
            </tr>
          </thead>
          <tbody>
            {machines.map((m) => {
              const isSelected = !!selected.find((s) => s.id === m.id);
              const share = isSelected && totalPower > 0
                ? Math.round((powerScore(m) / totalPower) * 100)
                : null;
              return (
                <tr
                  key={m.id}
                  className={isSelected ? "row-selected" : ""}
                  onClick={() => toggle(m)}
                  style={{ cursor: "pointer" }}
                >
                  <td>
                    <span className={`check-box ${isSelected ? "checked" : ""}`}>
                      {isSelected ? "✓" : ""}
                    </span>
                  </td>
                  <td>{m.gpu_model}</td>
                  <td>{m.gpu_vram_gb} GB</td>
                  <td>{m.cpu_cores}</td>
                  <td>{m.ram_gb} GB</td>
                  {selected.length > 0 && (
                    <td>{share !== null ? `~${share}%` : "—"}</td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// -----------------------------------------------
// Page: Submit Job (multi-machine)
// -----------------------------------------------
function SubmitJobPage({ machines, onBack, onSubmitted }) {
  const [file, setFile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState(null);
  // Manual frame range (shown when auto-parse fails)
  const [needsFrameInput, setNeedsFrameInput] = useState(false);
  const [pendingGroupId, setPendingGroupId] = useState(null);
  const [frameStart, setFrameStart] = useState("1");
  const [frameEnd, setFrameEnd] = useState("250");
  const [frameStep, setFrameStep] = useState("1");

  const powerScore = (m) =>
    (m.gpu_vram_gb || 0) * 4 + (m.cpu_cores || 0) + (m.ram_gb || 0) * 0.3;
  const totalPower = machines.reduce((s, m) => s + powerScore(m), 0);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!file) return setError("Select a .blend file or .zip project bundle first");
    setLoading(true);
    setError(null);
    setProgress(0);
    try {
      const result = await submitDistributedJob(
        machines.map((m) => m.id),
        file,
        (pct) => {
          setProgress(pct);
          if (pct >= 100) setAnalyzing(true);
        }
      );
      if (result.needs_frame_input) {
        // Auto-parse failed — ask user for frame range
        setPendingGroupId(result.group_id);
        setNeedsFrameInput(true);
        setLoading(false);
        setAnalyzing(false);
      } else {
        onSubmitted(result.group_id, result);
      }
    } catch (err) {
      setError(err.message);
      setLoading(false);
      setAnalyzing(false);
    }
  };

  const handleFrameConfirm = async (e) => {
    e.preventDefault();
    const fs = parseInt(frameStart, 10);
    const fe = parseInt(frameEnd, 10);
    const fst = parseInt(frameStep, 10) || 1;
    if (isNaN(fs) || isNaN(fe) || fe < fs) return setError("Invalid frame range");
    setLoading(true);
    setError(null);
    try {
      const result = await confirmDistributedJob(
        pendingGroupId,
        machines.map((m) => m.id),
        { frame_start: fs, frame_end: fe, frame_step: fst }
      );
      onSubmitted(result.group_id, result);
    } catch (err) {
      setError(err.message);
      setLoading(false);
    }
  };

  const MachineSummary = () => (
    <div className="machine-card" style={{ marginBottom: 16 }}>
      <strong>{machines.length} machine{machines.length > 1 ? "s" : ""} selected</strong>
      <div className="dist-preview-bar" style={{ margin: "10px 0 4px" }}>
        {machines.map((m, i) => {
          const share = totalPower > 0 ? (powerScore(m) / totalPower) * 100 : 0;
          return (
            <div
              key={m.id}
              style={{
                width: `${Math.max(share, 3)}%`,
                height: 8,
                background: SEGMENT_COLORS[i % SEGMENT_COLORS.length],
                borderRadius: i === 0 ? "4px 0 0 4px" : i === machines.length - 1 ? "0 4px 4px 0" : 0,
                display: "inline-block",
              }}
              title={`${m.gpu_model}: ~${Math.round(share)}%`}
            />
          );
        })}
      </div>
      {machines.map((m, i) => {
        const share = totalPower > 0 ? Math.round((powerScore(m) / totalPower) * 100) : 0;
        return (
          <div key={m.id} style={{ fontSize: 11, color: "#888", display: "flex", gap: 6, alignItems: "center" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: SEGMENT_COLORS[i % SEGMENT_COLORS.length], display: "inline-block" }} />
            <span>{m.gpu_model}</span>
            <span>~{share}% of frames</span>
          </div>
        );
      })}
    </div>
  );

  if (needsFrameInput) {
    return (
      <div>
        <button className="btn-back" onClick={onBack}>← Back</button>
        <h2>Enter Frame Range</h2>
        <MachineSummary />
        <p className="status" style={{ marginBottom: 16 }}>
          Could not auto-detect frame range from your .blend file (Blender 5.0+ format). Enter it manually from your Blender scene settings.
        </p>
        <form onSubmit={handleFrameConfirm} className="submit-form">
          <label>
            Start Frame
            <input type="number" value={frameStart} onChange={(e) => setFrameStart(e.target.value)} min="1" style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc" }} />
          </label>
          <label>
            End Frame
            <input type="number" value={frameEnd} onChange={(e) => setFrameEnd(e.target.value)} min="1" style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc" }} />
          </label>
          <label>
            Frame Step
            <input type="number" value={frameStep} onChange={(e) => setFrameStep(e.target.value)} min="1" style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc" }} />
          </label>
          {error && <p className="error">{error}</p>}
          <button className="btn-primary" type="submit" disabled={loading}>
            {loading ? "Starting..." : `Start Distributed Render (${machines.length} machines)`}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div>
      <button className="btn-back" onClick={onBack}>← Back</button>
      <h2>Distributed Render Job</h2>
      <MachineSummary />
      <form onSubmit={handleSubmit} className="submit-form">
        <label>
          Project File (.blend or .zip)
          <input
            type="file"
            accept=".blend,.zip"
            onChange={(e) => setFile(e.target.files[0])}
          />
        </label>
        <p className="status">
          Use a single `.blend` only if textures are packed. Otherwise upload a `.zip` with the full project folder.
        </p>
        {file && <p className="file-name">{file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)</p>}
        {error && <p className="error">{error}</p>}
        {loading && (
          <div className="progress-bar-wrap">
            <div className="progress-bar" style={{ width: `${progress}%` }} />
            <span className="progress-text">
              {analyzing ? "Analyzing frames..." : `Uploading... ${progress}%`}
            </span>
          </div>
        )}
        <button className="btn-primary" type="submit" disabled={loading}>
          {loading
            ? analyzing ? "Analyzing..." : `Uploading... ${progress}%`
            : `Start Distributed Render (${machines.length} machines)`}
        </button>
      </form>
    </div>
  );
}

// -----------------------------------------------
// Page: Render Group Status
// -----------------------------------------------
const STATUS_LABEL = {
  pending: "Waiting for machines...",
  running: "Rendering...",
  done: "Done!",
  failed: "Failed",
};

function RenderGroupStatusPage({ groupId, initialGroup, onBack }) {
  const [group, setGroup] = useState(initialGroup || null);
  const [error, setError] = useState(null);
  const intervalRef = useRef();

  const fetchGroup = async () => {
    try {
      const g = await getRenderGroup(groupId);
      setGroup(g);
      if (g.status === "done" || g.status === "failed") {
        clearInterval(intervalRef.current);
      }
    } catch {
      setError("Failed to fetch render status");
    }
  };

  useEffect(() => {
    if (!initialGroup) fetchGroup();
    intervalRef.current = setInterval(fetchGroup, 3000);
    return () => clearInterval(intervalRef.current);
  }, [groupId]);

  if (error) return <p className="error">{error}</p>;
  if (!group) return <p className="status">Loading...</p>;

  const statusLabel = STATUS_LABEL[group.status] || group.status;
  const overallPct =
    group.status === "done" ? 100
    : typeof group.overall_progress_pct === "number"
    ? Math.max(0, Math.min(100, group.overall_progress_pct))
    : null;
  const tasksDone = (group.tasks || []).filter((t) => t.status === "done").length;
  const taskCount = (group.tasks || []).length;

  return (
    <div>
      <button className="btn-back" onClick={onBack}>← Browse Machines</button>
      <h2>Distributed Render Status</h2>
      <div className="job-card">
        <div className="job-id">Group: <code>{groupId.slice(0, 8)}...</code></div>
        <div className={`status-badge status-${group.status}`}>{statusLabel}</div>

        {group.tasks && group.tasks.length > 0 && (
          <div className="render-progress-wrap" style={{ marginTop: 16 }}>
            <SegmentedProgressBar tasks={group.tasks} totalFrames={group.total_frames} />
            <div className="render-progress-meta">
              <span>
                {typeof group.total_frames === "number"
                  ? `${group.overall_rendered_frames || 0} / ${group.total_frames} frames`
                  : "Analyzing..."}
              </span>
              <span>
                {overallPct !== null ? `${Math.round(overallPct)}%` : "Working..."}
                {tasksDone > 0 && ` · ${tasksDone}/${taskCount} machines done`}
              </span>
            </div>
          </div>
        )}

        {group.status === "failed" && group.error && (
          <p className="error"><strong>Error:</strong> {group.error}</p>
        )}

        {group.status === "done" && (
          <div className="done-section">
            <p>All {taskCount} chunks rendered successfully</p>
            <a className="btn-primary" href={renderGroupDownloadUrl(groupId)} download>
              Download All Frames
            </a>
          </div>
        )}

        <div className="job-meta">
          <span>Submitted: {new Date(group.submitted_at).toLocaleString()}</span>
          {group.completed_at && (
            <span>Completed: {new Date(group.completed_at).toLocaleString()}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------
// App Shell
// -----------------------------------------------
export default function App() {
  const [page, setPage] = useState("machines");
  const [selectedMachines, setSelectedMachines] = useState([]);
  const [groupId, setGroupId] = useState(null);
  const [initialGroup, setInitialGroup] = useState(null);

  const handleContinue = (machines) => {
    setSelectedMachines(machines);
    setPage("submit");
  };

  const handleSubmitted = (id, groupData) => {
    setGroupId(id);
    setInitialGroup(groupData);
    setPage("status");
  };

  return (
    <div className="app">
      <header>
        <h1 onClick={() => setPage("machines")} style={{ cursor: "pointer" }}>
          PC Rent
        </h1>
        <span className="tagline">Distributed rendering marketplace</span>
      </header>
      <main>
        {page === "machines" && <MachinesPage onContinue={handleContinue} />}
        {page === "submit" && selectedMachines.length > 0 && (
          <SubmitJobPage
            machines={selectedMachines}
            onBack={() => setPage("machines")}
            onSubmitted={handleSubmitted}
          />
        )}
        {page === "status" && groupId && (
          <RenderGroupStatusPage
            groupId={groupId}
            initialGroup={initialGroup}
            onBack={() => setPage("machines")}
          />
        )}
      </main>
    </div>
  );
}
