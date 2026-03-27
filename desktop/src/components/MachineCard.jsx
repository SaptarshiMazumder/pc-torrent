export default function MachineCard({ machine, selected, onToggle }) {
  return (
    <div
      className={`card machine-card ${selected ? "machine-card-selected" : ""}`}
      onClick={() => onToggle(machine)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onToggle(machine)}
    >
      <div className="machine-card-body">
        <div className="machine-card-header">
          <div className="machine-gpu-name">{machine.gpu_model}</div>
          <div className={`machine-select-check ${selected ? "checked" : ""}`}>
            {selected ? "\u2713" : ""}
          </div>
        </div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">VRAM</span>
            <span className="info-value">{machine.gpu_vram_gb} GB</span>
          </div>
          <div className="info-item">
            <span className="info-label">CPU</span>
            <span className="info-value">{machine.cpu_cores} cores</span>
          </div>
          <div className="info-item">
            <span className="info-label">RAM</span>
            <span className="info-value">{machine.ram_gb} GB</span>
          </div>
        </div>
      </div>
    </div>
  );
}
