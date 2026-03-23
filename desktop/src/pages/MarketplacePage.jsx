import { useState, useEffect } from "react";
import { getMachines, submitJob } from "../lib/api";
import MachineCard from "../components/MachineCard";

export default function MarketplacePage({ backendUrl, onJobSubmitted }) {
  const [view, setView] = useState("list");
  const [machines, setMachines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedMachine, setSelectedMachine] = useState(null);

  // Submit state
  const [file, setFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [submitError, setSubmitError] = useState(null);

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

  const handleRent = (machine) => {
    setSelectedMachine(machine);
    setFile(null);
    setUploading(false);
    setProgress(0);
    setSubmitError(null);
    setView("submit");
  };

  const handleBack = () => {
    setView("list");
    setSelectedMachine(null);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!file) return setSubmitError("Select a .blend or .zip file first");
    setUploading(true);
    setSubmitError(null);
    setProgress(0);
    try {
      const job = await submitJob(backendUrl, selectedMachine.id, file, setProgress);
      onJobSubmitted(job.job_id, selectedMachine.gpu_model, file.name);
    } catch (err) {
      setSubmitError(err.message);
      setUploading(false);
    }
  };

  // ---- List View ----
  if (view === "list") {
    return (
      <div className="page">
        <div className="page-header">
          <h2>Marketplace</h2>
          <button className="btn btn-secondary" onClick={loadMachines} disabled={loading}>
            Refresh
          </button>
        </div>

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
              <MachineCard key={m.id} machine={m} onRent={handleRent} />
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
        ← Back to Marketplace
      </button>
      <h2>Submit Render Job</h2>

      <div className="card selected-machine-card">
        <div className="machine-gpu-name">{selectedMachine.gpu_model}</div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">VRAM</span>
            <span className="info-value">{selectedMachine.gpu_vram_gb} GB</span>
          </div>
          <div className="info-item">
            <span className="info-label">CPU</span>
            <span className="info-value">{selectedMachine.cpu_cores} cores</span>
          </div>
          <div className="info-item">
            <span className="info-label">RAM</span>
            <span className="info-value">{selectedMachine.ram_gb} GB</span>
          </div>
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
            {file.name} — {(file.size / 1024 / 1024).toFixed(1)} MB
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
              <span>Uploading...</span>
              <span>{progress}%</span>
            </div>
          </div>
        )}

        <button
          className="btn btn-primary submit-btn"
          type="submit"
          disabled={uploading}
        >
          {uploading ? `Uploading... ${progress}%` : "Start Render"}
        </button>
      </form>
    </div>
  );
}
