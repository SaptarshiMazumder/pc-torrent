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

const RENTER_PAGES = [
  { id: "dashboard", label: "Dashboard", icon: "\u2B21" },
  { id: "logs", label: "Logs", icon: "\u2630" },
];

const RENTEE_PAGES = [
  { id: "create", label: "Create Render", icon: "\u25CE" },
  { id: "myjobs", label: "My Jobs", icon: "\u25A4" },
  { id: "available", label: "Available Machines", icon: "\u2394" },
  { id: "logs", label: "Logs", icon: "\u2630" },
];

import { useAuth } from "../contexts/AuthContext";

export default function Sidebar({ activePage, onNavigate, status, mode, onModeChange }) {
  const { user, signOut } = useAuth();
  const pages = mode === "renter" ? RENTER_PAGES : RENTEE_PAGES;
  const dotColor = STATUS_COLORS[status] || "#6b7280";

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">
        <span className="brand-icon">{"\u25C8"}</span>
        <span className="brand-text">PC Rent</span>
      </div>

      <div className="mode-select-wrap">
        <select
          className="mode-select"
          value={mode}
          onChange={(e) => onModeChange(e.target.value)}
        >
          <option value="rentee">Rent a PC</option>
          <option value="renter">Offer My PC</option>
        </select>
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

        <div className="nav-divider" />

        <button
          className={`nav-item ${activePage === "settings" ? "active" : ""}`}
          onClick={() => onNavigate("settings")}
        >
          <span className="nav-icon">{"\u2699"}</span>
          <span className="nav-label">Settings</span>
        </button>
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
        {user && (
          <div className="sidebar-user">
            <span className="sidebar-email">{user.email}</span>
            <button className="btn-link" onClick={signOut}>Sign out</button>
          </div>
        )}
        <div className="version-label">v1.0.0</div>
      </div>
    </nav>
  );
}
