const STATUS_MAP = {
  disconnected: { color: "#6b7280", label: "Offline" },
  connected: { color: "#22c55e", label: "Available" },
  rendering: { color: "#3b82f6", label: "Rendering" },
  paused: { color: "#f59e0b", label: "Paused" },
  error: { color: "#ef4444", label: "Error" },
  checking_requirements: { color: "#a78bfa", label: "Checking System" },
  setting_up_docker: { color: "#a78bfa", label: "Setting Up Docker" },
  downloading_image: { color: "#a78bfa", label: "Downloading Image" },
  registering: { color: "#a78bfa", label: "Registering" },
  needs_reboot: { color: "#f59e0b", label: "Reboot Required" },
};

export default function StatusIndicator({ status, message }) {
  const info = STATUS_MAP[status] || STATUS_MAP.disconnected;

  return (
    <div className="status-indicator">
      <span
        className={`status-dot ${status === "rendering" ? "pulse" : ""}`}
        style={{ backgroundColor: info.color }}
      />
      <div className="status-text">
        <span className="status-state">{info.label}</span>
        {message && <span className="status-message">{message}</span>}
      </div>
    </div>
  );
}
