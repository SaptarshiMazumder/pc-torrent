import { useState, useEffect, useRef, useCallback } from "react";
import {
  getMachines,
  createDistributedRenderGroup,
  uploadDistributedRenderInput,
  confirmDistributedJob,
  getRenderGroup,
  getRenderGroupOutputs,
  getFirebaseToken,
  buildAuthenticatedApiUrl,
  renderGroupDownloadUrl,
  logsStreamUrl,
  listInputFiles,
  renameInputFile,
  deleteInputFile,
} from "./api";
import { parseBlendFile } from "./lib/blend-parser";
import { useAuth } from "./contexts/AuthContext";
import LoginPage from "./components/LoginPage";
import "./App.css";

const SEGMENT_COLORS = [
  "#6c63ff", "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
];

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").trim();
}

function frameIndexFromFilename(filename) {
  if (typeof filename !== "string") return -1;
  const match = filename.match(/(\d+)(?=\.[^.]+$)/);
  if (!match) return -1;
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : -1;
}

function sortOutputEntries(a, b) {
  const frameA = frameIndexFromFilename(a?.filename || "");
  const frameB = frameIndexFromFilename(b?.filename || "");
  if (frameA !== frameB) return frameA - frameB;
  return String(a?.filename || "").localeCompare(String(b?.filename || ""));
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
              <span className="seg-workerid" style={{ fontFamily: "monospace", fontSize: 10, color: "#666", marginLeft: 2 }}>[{task.runpod_job_id || task.job_id.slice(0, 8)}]</span>
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
  const [savedInputs, setSavedInputs] = useState([]);
  const [savedInputsLoading, setSavedInputsLoading] = useState(false);
  const [savedInputsError, setSavedInputsError] = useState("");
  const [selectedSavedInputId, setSelectedSavedInputId] = useState("");
  const [flowStage, setFlowStage] = useState("idle");
  const [analyzing, setAnalyzing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState(null);
  const [analysisResult, setAnalysisResult] = useState(null);
  const [clientParseError, setClientParseError] = useState("");
  const [serverParseError, setServerParseError] = useState("");
  const [pendingGroupId, setPendingGroupId] = useState(null);
  const [frameStart, setFrameStart] = useState("");
  const [frameEnd, setFrameEnd] = useState("");
  const [frameStep, setFrameStep] = useState("1");

  const powerScore = (m) =>
    (m.gpu_vram_gb || 0) * 4 + (m.cpu_cores || 0) + (m.ram_gb || 0) * 0.3;
  const totalPower = machines.reduce((s, m) => s + powerScore(m), 0);

  const refreshSavedInputs = async () => {
    setSavedInputsLoading(true);
    setSavedInputsError("");
    try {
      const resp = await listInputFiles();
      setSavedInputs(Array.isArray(resp?.files) ? resp.files : []);
    } catch (err) {
      setSavedInputsError(err.message || "Failed to load saved files");
    } finally {
      setSavedInputsLoading(false);
    }
  };

  useEffect(() => {
    refreshSavedInputs();
  }, []);

  const applySavedInputPrefill = (asset) => {
    const prefill = asset?.prefill_frame_range || null;
    if (prefill) {
      setFrameStart(String(prefill.frame_start));
      setFrameEnd(String(prefill.frame_end));
      setFrameStep(String(prefill.frame_step || 1));
      setAnalysisResult({
        frame_start: prefill.frame_start,
        frame_end: prefill.frame_end,
        frame_step: prefill.frame_step || 1,
      });
      setClientParseError("");
    } else {
      setFrameStart("");
      setFrameEnd("");
      setFrameStep("1");
      setAnalysisResult(null);
      setClientParseError("Saved file has no frame metadata. Enter frame range manually.");
    }
  };

  const parseManualFrameRange = () => {
    const fs = parseInt(frameStart, 10);
    const fe = parseInt(frameEnd, 10);
    const fst = parseInt(frameStep, 10);
    if (Number.isNaN(fs) || Number.isNaN(fe) || fs < 1 || fe < fs) {
      return null;
    }
    return {
      frame_start: fs,
      frame_end: fe,
      frame_step: Number.isNaN(fst) || fst < 1 ? 1 : fst,
    };
  };

  const resetFlowForInputChange = () => {
    setFlowStage("idle");
    setAnalyzing(false);
    setUploading(false);
    setStarting(false);
    setProgress(0);
    setAnalysisResult(null);
    setClientParseError("");
    setServerParseError("");
    setPendingGroupId(null);
    setFrameStart("");
    setFrameEnd("");
    setFrameStep("1");
    setError(null);
  };

  const handleAnalyze = async () => {
    if (!file) {
      setError("Select a .blend file or .zip project bundle first");
      return;
    }

    setError(null);
    setProgress(0);
    setServerParseError("");
    setPendingGroupId(null);
    setAnalyzing(true);

    try {
      const parsed = await parseBlendFile(file);
      setAnalysisResult(parsed);
      setClientParseError("");
      setFrameStart(String(parsed.frame_start));
      setFrameEnd(String(parsed.frame_end));
      setFrameStep(String(parsed.frame_step || 1));
    } catch (err) {
      setAnalysisResult(null);
      setClientParseError(`Client parse failed: ${err.message || "Unknown parsing error"}`);
      setFrameStart("");
      setFrameEnd("");
      setFrameStep("1");
    } finally {
      setAnalyzing(false);
      setFlowStage("analyzed");
    }
  };

  const handleUpload = async () => {
    if (flowStage !== "analyzed") return;
    if (!file) {
      setError("Select a .blend file or .zip project bundle first");
      return;
    }

    setError(null);
    setServerParseError("");
    setProgress(0);
    setUploading(true);

    try {
      const created = await createDistributedRenderGroup(
        machines.map((m) => m.id),
        file.name,
        file.size
      );
      setPendingGroupId(created.group_id || null);

      await uploadDistributedRenderInput(created.group_id, file, (pct) => {
        setProgress(pct);
      });

      setFlowStage("uploaded");
    } catch (err) {
      setError(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleUseSavedInput = async () => {
    if (!selectedSavedInputId) {
      setError("Select a saved file first");
      return;
    }
    setError(null);
    setServerParseError("");
    setStarting(true);
    try {
      const created = await createDistributedRenderGroup(
        machines.map((m) => m.id),
        null,
        null,
        selectedSavedInputId
      );
      setPendingGroupId(created.group_id || null);
      applySavedInputPrefill(created.prefill || created.source_asset || null);
      setFlowStage("uploaded");
      setFile(null);
      await refreshSavedInputs();
    } catch (err) {
      setError(err.message || "Failed to create render group from saved file");
    } finally {
      setStarting(false);
    }
  };

  const handleRenameSavedInput = async (asset) => {
    const initialName = asset?.display_name || asset?.input_filename || "";
    const nextName = window.prompt("Rename saved file", initialName);
    if (nextName === null) return;
    const trimmed = nextName.trim();
    if (!trimmed) {
      setError("Name cannot be empty");
      return;
    }
    try {
      await renameInputFile(asset.id, trimmed);
      await refreshSavedInputs();
    } catch (err) {
      setError(err.message || "Failed to rename saved file");
    }
  };

  const handleDeleteSavedInput = async (asset) => {
    const label = asset?.display_name || asset?.input_filename || "this file";
    const ok = window.confirm(`Delete "${label}" from saved files?`);
    if (!ok) return;
    try {
      await deleteInputFile(asset.id);
      if (selectedSavedInputId === asset.id) {
        setSelectedSavedInputId("");
      }
      await refreshSavedInputs();
    } catch (err) {
      setError(err.message || "Failed to delete saved file");
    }
  };

  const handleStartRendering = async () => {
    if (flowStage !== "uploaded") return;
    if (!pendingGroupId) {
      setError("Upload must complete before starting render");
      return;
    }

    const frameRange = parseManualFrameRange();

    if (!frameRange) {
      setError("Enter a valid manual frame range before starting render");
      return;
    }

    setError(null);
    setServerParseError("");
    setStarting(true);
    setFlowStage("starting");

    try {
      const result = await confirmDistributedJob(
        pendingGroupId,
        machines.map((m) => m.id),
        frameRange
      );

      if (result.needs_frame_input) {
        const detail = result.parse_error
          ? `Server parse failed: ${result.parse_error}`
          : "Server requires manual frame range";
        setServerParseError(detail);
        setError("Could not start render. Provide frame range and retry.");
        setFlowStage("uploaded");
        return;
      }

      setFlowStage("submitted");
      await refreshSavedInputs();
      onSubmitted(result.group_id, result);
    } catch (err) {
      setError(err.message || "Failed to start render");
      setFlowStage("uploaded");
    } finally {
      setStarting(false);
    }
  };

  const handleFileChange = (event) => {
    setFile(event.target.files[0] || null);
    setSelectedSavedInputId("");
    resetFlowForInputChange();
  };

  const canUpload = flowStage === "analyzed" && !analyzing && !uploading && !starting;
  const manualRangeValid = parseManualFrameRange() !== null;
  const selectedSavedInput = savedInputs.find((asset) => asset.id === selectedSavedInputId) || null;
  const canUseSaved =
    !analyzing &&
    !uploading &&
    !starting &&
    flowStage !== "starting" &&
    !!selectedSavedInputId;
  const canStart =
    flowStage === "uploaded" &&
    !analyzing &&
    !uploading &&
    !starting &&
    !!pendingGroupId &&
    (analysisResult ? true : manualRangeValid);

  const stepLabel = analyzing ? "Analyzing" : uploading ? "Uploading" : starting ? "Starting" : "";

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

  return (
    <div>
      <button className="btn-back" onClick={onBack}>{"<- Back"}</button>
      <h2>Distributed Render Job</h2>
      <MachineSummary />

      <div className="submit-form">
        <div className="saved-inputs-panel">
          <div className="saved-inputs-head">
            <strong>Saved Files</strong>
            <button className="btn-secondary" type="button" onClick={refreshSavedInputs} disabled={savedInputsLoading || analyzing || uploading || starting}>
              {savedInputsLoading ? "Refreshing..." : "Refresh"}
            </button>
          </div>
          {savedInputsError && <p className="error">{savedInputsError}</p>}
          {savedInputsLoading && <p className="status">Loading saved files...</p>}
          {!savedInputsLoading && savedInputs.length === 0 && (
            <p className="status">No saved files yet. Upload and start one render to save it.</p>
          )}
          {savedInputs.map((asset) => (
            <div key={asset.id} className={`saved-input-row ${selectedSavedInputId === asset.id ? "selected" : ""}`}>
              <label className="saved-input-select">
                <input
                  type="radio"
                  name="saved-input"
                  checked={selectedSavedInputId === asset.id}
                  onChange={() => setSelectedSavedInputId(asset.id)}
                />
                <span>
                  {asset.display_name || asset.input_filename}
                  <small> ({asset.input_filename})</small>
                </span>
              </label>
              <div className="saved-input-actions">
                <button className="btn-secondary" type="button" onClick={() => handleRenameSavedInput(asset)} disabled={analyzing || uploading || starting}>
                  Rename
                </button>
                <button className="btn-secondary" type="button" onClick={() => handleDeleteSavedInput(asset)} disabled={analyzing || uploading || starting}>
                  Delete
                </button>
              </div>
            </div>
          ))}
          <button className="btn-primary" type="button" onClick={handleUseSavedInput} disabled={!canUseSaved}>
            {starting ? "Preparing..." : "Use Saved File"}
          </button>
          {selectedSavedInput && (
            <p className="status" style={{ paddingTop: 0 }}>
              Selected: {selectedSavedInput.display_name || selectedSavedInput.input_filename}
            </p>
          )}
        </div>

        <label>
          Project File (.blend or .zip)
          <input
            type="file"
            accept=".blend,.zip"
            onChange={handleFileChange}
          />
        </label>
        <p className="status">
          Use a single `.blend` only if textures are packed. Otherwise upload a `.zip` with the full project folder.
        </p>
        {file && <p className="file-name">{file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)</p>}

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {flowStage === "idle" && (
            <button
              className="btn-primary"
              type="button"
              onClick={handleAnalyze}
              disabled={!file || analyzing || uploading || starting}
            >
              {analyzing ? "Analyzing..." : "Analyze"}
            </button>
          )}

          {flowStage === "analyzed" && (
            <button className="btn-primary" type="button" onClick={handleUpload} disabled={!canUpload}>
              {uploading ? `Uploading... ${progress}%` : "Upload"}
            </button>
          )}

          {flowStage === "uploaded" && (
            <button className="btn-primary" type="button" onClick={handleStartRendering} disabled={!canStart}>
              {starting ? "Starting..." : "Start Rendering"}
            </button>
          )}

          {flowStage !== "idle" && (
            <button className="btn-secondary" type="button" onClick={resetFlowForInputChange} disabled={analyzing || uploading || starting}>
              Reset
            </button>
          )}
        </div>

        {(analyzing || uploading || starting) && (
          <div className="progress-bar-wrap">
            <div
              className="progress-bar"
              style={{ width: uploading ? `${progress}%` : "100%", opacity: uploading ? 1 : 0.75 }}
            />
            <span className="progress-text">
              {uploading ? `${stepLabel}... ${progress}%` : `${stepLabel}...`}
            </span>
          </div>
        )}

        {flowStage !== "idle" && (
          <div>
            <p className="status" style={{ paddingTop: 0 }}>
              {analysisResult
                ? `Analyzed ${file?.name || "file"}: frames ${analysisResult.frame_start}..${analysisResult.frame_end}${
                    (analysisResult.frame_step || 1) > 1
                      ? `, every ${analysisResult.frame_step} frames`
                      : ""
                  }`
                : "Analyze could not detect frames. Upload can continue, but manual frame range is required before Start Rendering."}
            </p>
            {clientParseError && <p className="error">{clientParseError}</p>}
            {serverParseError && <p className="error">{serverParseError}</p>}
          </div>
        )}

        {flowStage !== "idle" && (
          <>
            <label>
              Start Frame
              <input
                type="number"
                value={frameStart}
                onChange={(e) => setFrameStart(e.target.value)}
                min="1"
                style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc" }}
              />
            </label>
            <label>
              End Frame
              <input
                type="number"
                value={frameEnd}
                onChange={(e) => setFrameEnd(e.target.value)}
                min="1"
                style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc" }}
              />
            </label>
            <label>
              Frame Step
              <input
                type="number"
                value={frameStep}
                onChange={(e) => setFrameStep(e.target.value)}
                min="1"
                style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc" }}
              />
            </label>
          </>
        )}

        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}
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
  const [frameFiles, setFrameFiles] = useState([]);
  const [framesLoading, setFramesLoading] = useState(false);
  const [framesError, setFramesError] = useState("");
  const [viewer, setViewer] = useState(null);
  const intervalRef = useRef();
  const frameIntervalRef = useRef();

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

  const fetchFrames = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setFramesLoading(true);
    try {
      const token = await getFirebaseToken();
      const payload = await getRenderGroupOutputs(groupId);
      const files = Array.isArray(payload?.files) ? payload.files.slice().sort(sortOutputEntries) : [];
      const normalized = files.map((file) => ({
        ...file,
        preview_url: file?.preview_path
          ? buildAuthenticatedApiUrl(file.preview_path, token || "", file.size_bytes ?? file.filename)
          : file?.url || "",
      }));
      setFrameFiles(normalized);
      setFramesError("");
    } catch (err) {
      setFramesError(err?.message || "Failed to load frames");
    } finally {
      if (!silent) setFramesLoading(false);
    }
  }, [groupId]);

  useEffect(() => {
    if (!initialGroup) fetchGroup();
    intervalRef.current = setInterval(fetchGroup, 3000);
    return () => clearInterval(intervalRef.current);
  }, [groupId]);

  useEffect(() => {
    void fetchFrames({ silent: false });
    frameIntervalRef.current = setInterval(() => {
      void fetchFrames({ silent: true });
    }, 3000);
    return () => clearInterval(frameIntervalRef.current);
  }, [fetchFrames]);

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

        <div className="status-frame-gallery">
          <div className="status-frame-gallery-head">
            <strong>Live Frames</strong>
            <span>{frameFiles.length} available</span>
          </div>
          {framesLoading && frameFiles.length === 0 && (
            <p className="status">Loading frame previews...</p>
          )}
          {!framesLoading && !framesError && frameFiles.length === 0 && (
            <p className="status">No frames available yet.</p>
          )}
          {framesError && <p className="error">{framesError}</p>}
          {frameFiles.length > 0 && (
            <div className="status-frame-grid">
              {frameFiles.map((file) => {
                const fileKey = `${file.job_id || "job"}:${file.filename}`;
                return (
                  <button
                    key={fileKey}
                    type="button"
                    className="status-frame-tile"
                    onClick={() => {
                      setViewer({
                        filename: file.filename,
                        fullUrl: file.url,
                        previewUrl: file.preview_url,
                      });
                    }}
                  >
                    <img
                      className="status-frame-thumb"
                      src={file.preview_url}
                      alt={file.filename}
                      loading="lazy"
                    />
                    <span>{file.filename}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="job-meta">
          <span>Submitted: {new Date(group.submitted_at).toLocaleString()}</span>
          {group.completed_at && (
            <span>Completed: {new Date(group.completed_at).toLocaleString()}</span>
          )}
        </div>
      </div>
      {viewer && (
        <WebFrameViewerModal
          frame={viewer}
          onClose={() => setViewer(null)}
        />
      )}
    </div>
  );
}

function WebFrameViewerModal({ frame, onClose }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const draggingRef = useRef(null);

  useEffect(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }, [frame?.fullUrl]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
      if (event.key === "0") {
        setZoom(1);
        setOffset({ x: 0, y: 0 });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const applyZoom = (next) => setZoom(Math.max(1, Math.min(8, next)));

  const onWheel = (event) => {
    event.preventDefault();
    applyZoom(zoom + (event.deltaY > 0 ? -0.16 : 0.16));
  };

  const onMouseDown = (event) => {
    draggingRef.current = { x: event.clientX, y: event.clientY };
  };

  const onMouseMove = (event) => {
    if (!draggingRef.current) return;
    const dx = event.clientX - draggingRef.current.x;
    const dy = event.clientY - draggingRef.current.y;
    draggingRef.current = { x: event.clientX, y: event.clientY };
    setOffset((prev) => ({ x: prev.x + dx, y: prev.y + dy }));
  };

  const clearDrag = () => {
    draggingRef.current = null;
  };

  return (
    <div className="web-frame-viewer-modal" onClick={onClose}>
      <div className="web-frame-viewer-card" onClick={(event) => event.stopPropagation()}>
        <div className="web-frame-viewer-head">
          <strong>{frame?.filename || "Frame"}</strong>
          <div className="web-frame-viewer-actions">
            <button className="btn-secondary" type="button" onClick={() => applyZoom(zoom - 0.2)}>-</button>
            <button className="btn-secondary" type="button" onClick={() => applyZoom(zoom + 0.2)}>+</button>
            <button
              className="btn-secondary"
              type="button"
              onClick={() => {
                setZoom(1);
                setOffset({ x: 0, y: 0 });
              }}
            >
              Reset
            </button>
            <button className="btn-secondary" type="button" onClick={onClose}>Close</button>
          </div>
        </div>
        <div
          className="web-frame-viewer-canvas"
          onWheel={onWheel}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={clearDrag}
          onMouseLeave={clearDrag}
        >
          <img
            className="web-frame-viewer-image"
            src={frame?.fullUrl || frame?.previewUrl || ""}
            alt={frame?.filename || "Frame"}
            draggable={false}
            style={{
              transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
              cursor: zoom > 1 ? "grab" : "default",
            }}
          />
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------
// Page: Server Logs (SSE stream)
// -----------------------------------------------
function ServerLogsPage({ onBack }) {
  const [lines, setLines] = useState([]);
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef(null);
  const containerRef = useRef(null);

  useEffect(() => {
    let es;
    logsStreamUrl().then((url) => {
      es = new EventSource(url);
      es.onopen = () => setConnected(true);
      es.onmessage = (e) => {
        setLines((prev) => {
          const next = [...prev, e.data];
          return next.length > 1000 ? next.slice(-1000) : next;
        });
      };
      es.onerror = () => setConnected(false);
    });
    return () => es?.close();
  }, []);

  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [lines]);

  return (
    <div>
      <div className="page-header">
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button className="btn-back" onClick={onBack}>{"<- Back"}</button>
          <h2>Server Logs</h2>
        </div>
        <span style={{ fontSize: 12, color: connected ? "#10b981" : "#ef4444" }}>
          {connected ? "Live" : "Disconnected"}
        </span>
      </div>
      <div
        ref={containerRef}
        style={{
          background: "#0a0a0a",
          border: "1px solid #222",
          borderRadius: 8,
          padding: "12px 16px",
          fontFamily: "monospace",
          fontSize: 11,
          lineHeight: 1.6,
          maxHeight: "75vh",
          overflowY: "auto",
          color: "#ccc",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
        }}
      >
        {lines.length === 0 && <span style={{ color: "#666" }}>Waiting for logs...</span>}
        {lines.map((line, i) => (
          <div
            key={i}
            style={{
              color: line.includes("ERROR") ? "#ef4444"
                : line.includes("WARNING") ? "#f59e0b"
                : "#ccc",
            }}
          >
            {line}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// -----------------------------------------------
// App Shell
// -----------------------------------------------
export default function App() {
  const { user, loading, signOut } = useAuth();
  const [page, setPage] = useState("machines");
  const [selectedMachines, setSelectedMachines] = useState([]);
  const [groupId, setGroupId] = useState(null);
  const [initialGroup, setInitialGroup] = useState(null);

  if (loading) {
    return <div className="auth-loading">Loading...</div>;
  }

  if (!user) {
    return <LoginPage />;
  }

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
        <button
          className="btn-secondary"
          style={{ marginLeft: "auto", fontSize: 12 }}
          onClick={() => setPage("logs")}
        >
          Server Logs
        </button>
        <button
          className="btn-secondary"
          style={{ fontSize: 12 }}
          onClick={signOut}
        >
          Sign out
        </button>
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
        {page === "logs" && (
          <ServerLogsPage onBack={() => setPage("machines")} />
        )}
      </main>
    </div>
  );
}
