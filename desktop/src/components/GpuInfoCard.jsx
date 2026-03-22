export default function GpuInfoCard({ systemInfo }) {
  if (!systemInfo) {
    return (
      <div className="card gpu-card">
        <h3>System Info</h3>
        <p className="muted">Connect to detect system specs</p>
      </div>
    );
  }

  return (
    <div className="card gpu-card">
      <h3>System Info</h3>
      <div className="info-grid">
        <div className="info-item">
          <span className="info-label">GPU</span>
          <span className="info-value">
            {systemInfo.gpu_name || "Not detected"}
            {systemInfo.gpu_vram_gb > 0 && (
              <span className="info-sub"> ({systemInfo.gpu_vram_gb} GB VRAM)</span>
            )}
          </span>
        </div>
        <div className="info-item">
          <span className="info-label">CPU</span>
          <span className="info-value">{systemInfo.cpu_cores} cores</span>
        </div>
        <div className="info-item">
          <span className="info-label">RAM</span>
          <span className="info-value">{systemInfo.ram_gb} GB</span>
        </div>
        <div className="info-item">
          <span className="info-label">OS</span>
          <span className="info-value">{systemInfo.os_version || "Unknown"}</span>
        </div>
        <div className="info-item">
          <span className="info-label">Driver</span>
          <span className="info-value">{systemInfo.nvidia_driver || "N/A"}</span>
        </div>
      </div>
      {systemInfo.issues && systemInfo.issues.length > 0 && (
        <div className="issues-list">
          {systemInfo.issues.map((issue, i) => (
            <div key={i} className="issue-item">{issue}</div>
          ))}
        </div>
      )}
    </div>
  );
}
