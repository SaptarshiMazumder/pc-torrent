import { useCallback, useEffect, useMemo, useState } from "react";
import { getMachines } from "../lib/api";

function machineTypeLabel(machine) {
  if (machine?.machine_type === "runpod_serverless") return "Farm Endpoint";
  return "Desktop Worker";
}

export default function AvailableMachinesPage({ backendUrl }) {
  const [machines, setMachines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadMachines = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getMachines(backendUrl);
      setMachines(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(err?.message || "Failed to load available machines");
      setMachines([]);
    } finally {
      setLoading(false);
    }
  }, [backendUrl]);

  useEffect(() => {
    void loadMachines();
  }, [loadMachines]);

  const summary = useMemo(() => {
    const total = machines.length;
    const farms = machines.filter((machine) => machine?.machine_type === "runpod_serverless").length;
    const desktops = Math.max(0, total - farms);
    return { total, farms, desktops };
  }, [machines]);

  return (
    <div className="page">
      <div className="page-header">
        <h2>Available Machines</h2>
        <div className="page-header-actions">
          <button className="btn btn-secondary" onClick={() => { void loadMachines(); }} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      <div className="selection-summary" style={{ marginBottom: 16 }}>
        <span className="selection-count">{summary.total} online</span>
        <span className="selection-hint">
          {summary.desktops} desktop workers - {summary.farms} farm endpoints
        </span>
      </div>

      {error && <p className="error-text">{error}</p>}

      {loading ? (
        <div className="empty-state">
          <p>Loading available machines...</p>
        </div>
      ) : machines.length === 0 ? (
        <div className="empty-state">
          <p>No machines are available right now.</p>
        </div>
      ) : (
        <div className="machine-list">
          {machines.map((machine) => (
            <div key={machine.id} className="card machine-card" style={{ cursor: "default" }}>
              <div className="machine-card-body">
                <div className="machine-card-header">
                  <div className="machine-gpu-name">{machine.gpu_model || "Unknown GPU"}</div>
                  <span className="status-badge status-done">{machineTypeLabel(machine)}</span>
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
          ))}
        </div>
      )}
    </div>
  );
}

