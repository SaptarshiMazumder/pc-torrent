export default function MachineCard({ machine, onRent }) {
  return (
    <div className="card machine-card">
      <div className="machine-card-body">
        <div className="machine-gpu-name">{machine.gpu_model}</div>
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
      <button className="btn btn-primary" onClick={() => onRent(machine)}>
        Rent
      </button>
    </div>
  );
}
