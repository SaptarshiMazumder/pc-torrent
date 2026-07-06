const SPEC_TONES = {
  gpu: "teal",
  cpu: "blue",
  ram: "violet",
  os: "green",
  driver: "amber",
};

function SpecRow({ tone, label, value }) {
  return (
    <div className="dash-spec-row">
      <span className={`dash-spec-dot dash-spec-dot--${tone}`} />
      <span className="dash-spec-label">{label}</span>
      <span className="dash-spec-value">{value}</span>
    </div>
  );
}

export default function GpuInfoCard({ systemInfo }) {
  if (!systemInfo) {
    return (
      <div className="card gpu-card">
        <div className="dash-panel-head">
          <span className="dash-panel-title">System Info</span>
        </div>
        <p className="muted">Connect to detect system specs</p>
      </div>
    );
  }

  return (
    <div className="card gpu-card">
      <div className="dash-panel-head">
        <span className="dash-panel-title">System Info</span>
      </div>
      <div className="dash-spec-rows">
        <SpecRow
          tone={SPEC_TONES.gpu}
          label="GPU"
          value={
            <>
              {systemInfo.gpu_name || "Not detected"}
              {systemInfo.gpu_vram_gb > 0 && (
                <span className="info-sub"> ({systemInfo.gpu_vram_gb} GB VRAM)</span>
              )}
            </>
          }
        />
        <SpecRow tone={SPEC_TONES.cpu} label="CPU" value={`${systemInfo.cpu_cores} cores`} />
        <SpecRow tone={SPEC_TONES.ram} label="RAM" value={`${systemInfo.ram_gb} GB`} />
        <SpecRow tone={SPEC_TONES.os} label="OS" value={systemInfo.os_version || "Unknown"} />
        <SpecRow tone={SPEC_TONES.driver} label="Driver" value={systemInfo.nvidia_driver || "N/A"} />
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
