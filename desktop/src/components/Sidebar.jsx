const STATUS_COLORS = {
  connected: "#22c55e",
  rendering: "#3b82f6",
  paused: "#f59e0b",
  error: "#ef4444",
  disconnected: "#6b7280",
  checking_requirements: "#a78bfa",
  setting_up_docker: "#a78bfa",
  downloading_image: "#a78bfa",
  registering: "#a78bfa",
  needs_reboot: "#f59e0b",
};

export default function Sidebar({ activePage, onNavigate, status }) {
  const pages = [
    { id: "dashboard", label: "Dashboard", icon: "⬡" },
    { id: "logs", label: "Logs", icon: "☰" },
    { id: "settings", label: "Settings", icon: "⚙" },
  ];

  const dotColor = STATUS_COLORS[status] || "#6b7280";

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">
        <span className="brand-icon">◈</span>
        <span className="brand-text">PC Rent</span>
      </div>

      <div className="sidebar-nav">
        {pages.map((page) => (
          <button
            key={page.id}
            className={`nav-item ${activePage === page.id ? "active" : ""}`}
            onClick={() => onNavigate(page.id)}
          >
            <span className="nav-icon">{page.icon}</span>
            <span className="nav-label">{page.label}</span>
          </button>
        ))}
      </div>

      <div className="sidebar-footer">
        <div className="status-dot-row">
          <span className="status-dot" style={{ backgroundColor: dotColor }} />
          <span className="status-label">
            {status === "connected"
              ? "Available"
              : status === "rendering"
                ? "Rendering"
                : status === "paused"
                  ? "Paused"
                  : status === "disconnected"
                    ? "Offline"
                    : status === "error"
                      ? "Error"
                      : "Setting up..."}
          </span>
        </div>
        <div className="version-label">v1.0.0</div>
      </div>
    </nav>
  );
}
