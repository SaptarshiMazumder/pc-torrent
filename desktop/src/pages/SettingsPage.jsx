import { useState } from "react";

export default function SettingsPage({ backendUrl, onBackendUrlChange }) {
  const [url, setUrl] = useState(backendUrl);
  const [saved, setSaved] = useState(false);

  const handleSave = () => {
    onBackendUrlChange(url);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div className="page settings-page">
      <h2>Settings</h2>

      <div className="card">
        <h3>Backend Server</h3>
        <div className="setting-row">
          <label htmlFor="backend-url">Server URL</label>
          <div className="input-group">
            <input
              id="backend-url"
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://localhost:8000"
            />
            <button className="btn btn-primary" onClick={handleSave}>
              {saved ? "Saved!" : "Save"}
            </button>
          </div>
          <p className="setting-hint">
            The URL of the PC-Torrenter backend server that manages render jobs.
          </p>
        </div>
      </div>

      <div className="card">
        <h3>About</h3>
        <div className="about-info">
          <div className="info-item">
            <span className="info-label">Version</span>
            <span className="info-value">1.0.0</span>
          </div>
          <div className="info-item">
            <span className="info-label">Agent</span>
            <span className="info-value">Python Sidecar</span>
          </div>
        </div>
      </div>
    </div>
  );
}
