import { useState, useEffect } from "react";
import { getMachines, submitDistributedJob, confirmDistributedJob } from "../lib/api";
import MachineCard from "../components/MachineCard";

export default function MarketplacePage({ backendUrl, onJobSubmitted }) {
  const [view, setView] = useState("list");
  const [machines, setMachines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedMachines, setSelectedMachines] = useState([]);

  // Submit state
  const [file, setFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [submitError, setSubmitError] = useState(null);
  const [analyzing, setAnalyzing] = useState(false);

  // Manual frame range (shown when auto-parse fails)
  const [pendingGroupId, setPendingGroupId] = useState(null);
  const [frameStart, setFrameStart] = useState("1");
  const [frameEnd, setFrameEnd] = useState("250");
  const [frameStep, setFrameStep] = useState("1");

  const loadMachines = () => {
    setLoading(true);
    setError(null);
    getMachines(backendUrl)
      .then(setMachines)
      .catch(() => setError("Cannot reach server"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadMachines();
  }, [backendUrl]);

  const toggleMachine = (machine) => {
    setSelectedMachines((prev) => {
      const exists = prev.find((m) => m.id === machine.id);
      if (exists) return prev.filter((m) => m.id !== machine.id);
      return [...prev, machine];
    });
  };

  const handleContinue = () => {
    if (selectedMachines.length === 0) return;
    setFile(null);
    setUploading(false);
    setProgress(0);
    setSubmitError(null);
    setAnalyzing(false);
    setView("submit");
  };

  const handleBack = () => {
    setView("list");
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!file) return setSubmitError("Select a .blend or .zip file first");
    setUploading(true);
    setSubmitError(null);
    setProgress(0);
    try {
      const machineIds = selectedMachines.map((m) => m.id);
      setAnalyzing(false);
      const result = await submitDistributedJob(
        backendUrl,
        machineIds,
        file,
        (pct) => {
          setProgress(pct);
          if (pct >= 100) setAnalyzing(true);
        }
      );
      if (result.needs_frame_input) {
        setPendingGroupId(result.group_id);
        setView("frame-input");
        setUploading(false);
        setAnalyzing(false);
      } else {
        onJobSubmitted(result.group_id, file.name, result.tasks, result.total_frames);
      }
    } catch (err) {
      setSubmitError(err.message);
      setUploading(false);
      setAnalyzing(false);
    }
  };

  const handleFrameConfirm = async (e) => {
    e.preventDefault();
    const fs = parseInt(frameStart, 10);
    const fe = parseInt(frameEnd, 10);
    const fst = parseInt(frameStep, 10) || 1;
    if (isNaN(fs) || isNaN(fe) || fe < fs) return setSubmitError("Invalid frame range");
    setUploading(true);
    setSubmitError(null);
    try {
      const machineIds = selectedMachines.map((m) => m.id);
      const result = await confirmDistributedJob(backendUrl, pendingGroupId, machineIds, {
        frame_start: fs,
        frame_end: fe,
        frame_step: fst,
      });
      onJobSubmitted(result.group_id, file.name, result.tasks, result.total_frames);
    } catch (err) {
      setSubmitError(err.message);
      setUploading(false);
    }
  };

  // Power score for preview
  const powerScore = (m) =>
    (m.gpu_vram_gb || 0) * 4 + (m.cpu_cores || 0) * 1 + (m.ram_gb || 0) * 0.3;
  const totalPower = selectedMachines.reduce((s, m) => s + powerScore(m), 0);

  // ---- Frame Input View ----
  if (view === "frame-input") {
    return (
      <div className="page">
        <button className="btn-back" onClick={() => setView("list")}>&larr; Back to Marketplace</button>
        <h2>Enter Frame Range</h2>
        <p className="muted" style={{ marginBottom: 16 }}>
          Could not auto-detect frames from your .blend file (Blender 5.0+ format). Check your Blender Output Properties for the frame range.
        </p>
        <form onSubmit={handleFrameConfirm} className="submit-section">
          <div className="setting-row">
            <label>Start Frame</label>
            <input
              type="number" value={frameStart} min="1"
              onChange={(e) => setFrameStart(e.target.value)}
              style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc", width: 120 }}
            />
          </div>
          <div className="setting-row">
            <label>End Frame</label>
            <input
              type="number" value={frameEnd} min="1"
              onChange={(e) => setFrameEnd(e.target.value)}
              style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc", width: 120 }}
            />
          </div>
          <div className="setting-row">
            <label>Frame Step</label>
            <input
              type="number" value={frameStep} min="1"
              onChange={(e) => setFrameStep(e.target.value)}
              style={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6, padding: "0.5rem", color: "#ccc", width: 120 }}
            />
          </div>
          {submitError && <p className="error-text">{submitError}</p>}
          <button className="btn btn-primary submit-btn" type="submit" disabled={uploading}>
            {uploading ? "Starting..." : `Start Distributed Render (${selectedMachines.length} machines)`}
          </button>
        </form>
      </div>
    );
  }

  // ---- List View ----
  if (view === "list") {
    return (
      <div className="page">
        <div className="page-header">
          <h2>Marketplace</h2>
          <div className="page-header-actions">
            <button className="btn btn-secondary" onClick={loadMachines} disabled={loading}>
              Refresh
            </button>
            {selectedMachines.length > 0 && (
              <button className="btn btn-primary" onClick={handleContinue}>
                Rent {selectedMachines.length} machine{selectedMachines.length > 1 ? "s" : ""}
              </button>
            )}
          </div>
        </div>

        {selectedMachines.length > 0 && (
          <div className="selection-summary">
            <span className="selection-count">
              {selectedMachines.length} selected
            </span>
            <span className="selection-hint">
              Click machines to select/deselect. More machines = faster render.
            </span>
          </div>
        )}

        {loading && <p className="muted">Loading machines...</p>}
        {error && <p className="error-text">{error}</p>}
        {!loading && !error && machines.length === 0 && (
          <div className="empty-state">
            <p>No machines available right now.</p>
            <p className="muted">Check back shortly or hit Refresh.</p>
          </div>
        )}
        {!loading && !error && machines.length > 0 && (
          <div className="machine-list">
            {machines.map((m) => (
              <MachineCard
                key={m.id}
                machine={m}
                selected={!!selectedMachines.find((s) => s.id === m.id)}
                onToggle={toggleMachine}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  // ---- Submit View ----
  return (
    <div className="page">
      <button className="btn-back" onClick={handleBack}>
        &larr; Back to Marketplace
      </button>
      <h2>Distributed Render Job</h2>

      <div className="selected-machines-list">
        <h3>{selectedMachines.length} Machine{selectedMachines.length > 1 ? "s" : ""} Selected</h3>
        <div className="power-distribution-preview">
          {selectedMachines.map((m, i) => {
            const share = totalPower > 0 ? (powerScore(m) / totalPower) * 100 : 0;
            return (
              <div
                key={m.id}
                className="power-preview-segment"
                style={{ width: `${Math.max(share, 5)}%` }}
                title={`${m.gpu_model}: ~${Math.round(share)}% of frames`}
              >
                <div
                  className="power-preview-fill"
                  data-color-index={i % 6}
                />
              </div>
            );
          })}
        </div>
        <div className="power-preview-labels">
          {selectedMachines.map((m, i) => {
            const share = totalPower > 0 ? (powerScore(m) / totalPower) * 100 : 0;
            return (
              <div key={m.id} className="power-preview-label" data-color-index={i % 6}>
                <span className="power-label-dot" data-color-index={i % 6} />
                <span>{m.gpu_model}</span>
                <span className="muted">{Math.round(share)}%</span>
              </div>
            );
          })}
        </div>
      </div>

      <form onSubmit={handleSubmit} className="submit-section">
        <div className="setting-row">
          <label>Project File (.blend or .zip)</label>
          <div className="file-input-wrap">
            <label className="btn btn-secondary file-input-btn">
              {file ? file.name : "Choose file..."}
              <input
                type="file"
                accept=".blend,.zip"
                onChange={(e) => setFile(e.target.files[0])}
                hidden
              />
            </label>
          </div>
          <p className="setting-hint">
            Use a single .blend only if textures are packed. Otherwise upload a
            .zip with the full project folder.
          </p>
        </div>

        {file && (
          <p className="file-meta">
            {file.name} &mdash; {(file.size / 1024 / 1024).toFixed(1)} MB
          </p>
        )}

        {submitError && <p className="error-text">{submitError}</p>}

        {uploading && (
          <div className="runtime-progress-wrap">
            <div className="runtime-progress-track">
              <div
                className="runtime-progress-fill"
                style={{ width: `${progress}%` }}
              />
            </div>
            <div className="runtime-progress-meta">
              <span>{analyzing ? "Analyzing frames..." : "Uploading..."}</span>
              <span>{analyzing ? "Almost ready" : `${progress}%`}</span>
            </div>
          </div>
        )}

        <button
          className="btn btn-primary submit-btn"
          type="submit"
          disabled={uploading}
        >
          {uploading
            ? analyzing
              ? "Analyzing..."
              : `Uploading... ${progress}%`
            : `Start Distributed Render (${selectedMachines.length} machines)`}
        </button>
      </form>
    </div>
  );
}
