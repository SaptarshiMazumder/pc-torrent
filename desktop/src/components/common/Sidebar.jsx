import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../../contexts/AuthContext";

const STATUS_COLORS = {
  connected: "#22c55e",
  rendering: "#3b82f6",
  paused: "#f59e0b",
  error: "#ef4444",
  disconnected: "#6b7280",
  checking_requirements: "#f5a623",
  setting_up_docker: "#f5a623",
  downloading_image: "#f5a623",
  registering: "#f5a623",
  needs_reboot: "#f59e0b",
};

const STATUS_LABELS = {
  connected: "Available",
  rendering: "Rendering",
  paused: "Paused",
  disconnected: "Offline",
  error: "Error",
};

function CubeLogo() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="sidebar-brand-grad" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#e8724a" />
          <stop offset="100%" stopColor="#f5a623" />
        </linearGradient>
      </defs>
      <path
        d="M12 2L3 7v10l9 5 9-5V7l-9-5z"
        fill="rgba(232,114,74,0.12)"
        stroke="url(#sidebar-brand-grad)"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path
        d="M3 7l9 5 9-5M12 12v10"
        fill="none"
        stroke="url(#sidebar-brand-grad)"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ModeIconRent() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="6" width="20" height="14" rx="2" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  );
}

function ModeIconOffer() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </svg>
  );
}

const MODE_OPTIONS = [
  { value: "rentee", label: "Rent a PC", Icon: ModeIconRent },
  { value: "renter", label: "Offer My PC", Icon: ModeIconOffer },
];

function NavIconCreate() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v8M8 12h8" />
    </svg>
  );
}

function NavIconJobs() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  );
}

function NavIconDownloads() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="M7 10l5 5 5-5M12 15V3" />
    </svg>
  );
}

function NavIconMachines() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <rect x="9" y="9" width="6" height="6" />
      <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" />
    </svg>
  );
}

function NavIconLogs() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M3 6h18M3 12h18M3 18h12" />
    </svg>
  );
}

function NavIconDashboard() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12l9-9 9 9" />
      <path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10" />
    </svg>
  );
}

function NavIconSettings() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function SignOutIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5M21 12H9" />
    </svg>
  );
}

const RENTER_PAGES = [
  { id: "dashboard", label: "Dashboard", Icon: NavIconDashboard },
  { id: "logs", label: "Logs", Icon: NavIconLogs },
];

const RENTEE_PAGES = [
  { id: "create", label: "Create Render", Icon: NavIconCreate },
  { id: "myjobs", label: "My Jobs", Icon: NavIconJobs },
  { id: "downloads", label: "Downloads", Icon: NavIconDownloads },
  { id: "available", label: "Available Machines", Icon: NavIconMachines },
  { id: "logs", label: "Logs", Icon: NavIconLogs },
];

function useClickOutside(ref, handler, enabled) {
  useEffect(() => {
    if (!enabled) return undefined;
    function onDown(e) {
      if (ref.current && !ref.current.contains(e.target)) handler();
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [ref, handler, enabled]);
}

export default function Sidebar({
  activePage,
  onNavigate,
  status,
  mode,
  onModeChange,
  activeDownloadCount = 0,
}) {
  const { user, signOut } = useAuth();
  const pages = mode === "renter" ? RENTER_PAGES : RENTEE_PAGES;
  const dotColor = STATUS_COLORS[status] || "#6b7280";
  const statusText = STATUS_LABELS[status] || "Setting up...";

  const [modeOpen, setModeOpen] = useState(false);
  const modeWrapRef = useRef(null);
  const closeMode = useCallback(() => setModeOpen(false), []);
  useClickOutside(modeWrapRef, closeMode, modeOpen);

  const currentMode = MODE_OPTIONS.find((m) => m.value === mode) || MODE_OPTIONS[0];
  const CurrentModeIcon = currentMode.Icon;
  const initial = (user?.email || user?.displayName || "?").trim().charAt(0).toUpperCase();

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">
        <span className="sidebar-brand-mark">
          <CubeLogo />
        </span>
        <span className="sidebar-brand-text">PC Rent</span>
      </div>

      <div className="sidebar-mode" ref={modeWrapRef}>
        <button
          type="button"
          className="sidebar-mode-button"
          onClick={() => setModeOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={modeOpen}
        >
          <span className="sidebar-mode-icon"><CurrentModeIcon /></span>
          <span className="sidebar-mode-label">{currentMode.label}</span>
          <svg
            className={`sidebar-mode-chevron${modeOpen ? " open" : ""}`}
            width="12" height="12" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
        {modeOpen && (
          <div className="sidebar-mode-popover" role="menu">
            {MODE_OPTIONS.map((opt) => {
              const Icon = opt.Icon;
              const selected = opt.value === mode;
              return (
                <button
                  key={opt.value}
                  type="button"
                  role="menuitem"
                  className={`sidebar-mode-option${selected ? " selected" : ""}`}
                  onClick={() => {
                    onModeChange(opt.value);
                    setModeOpen(false);
                  }}
                >
                  <span className="sidebar-mode-icon"><Icon /></span>
                  <span className="sidebar-mode-label">{opt.label}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="sidebar-nav">
        {pages.map((page) => {
          const Icon = page.Icon;
          const active = activePage === page.id;
          return (
            <button
              key={page.id}
              type="button"
              className={`nav-item${active ? " nav-item-active" : ""}`}
              onClick={() => onNavigate(page.id)}
            >
              <span className="nav-icon"><Icon /></span>
              <span className="nav-label">{page.label}</span>
              {page.id === "downloads" && activeDownloadCount > 0 && (
                <span className="nav-badge">{activeDownloadCount}</span>
              )}
            </button>
          );
        })}

        <div className="nav-divider" />

        <button
          type="button"
          className={`nav-item${activePage === "settings" ? " nav-item-active" : ""}`}
          onClick={() => onNavigate("settings")}
        >
          <span className="nav-icon"><NavIconSettings /></span>
          <span className="nav-label">Settings</span>
        </button>
      </div>

      <div className="sidebar-footer">
        {user && (
          <div className="sidebar-user-chip">
            <span className="sidebar-avatar" aria-hidden="true">{initial}</span>
            <span className="sidebar-email" title={user.email || ""}>
              {user.email || user.displayName || "Signed in"}
            </span>
            <button
              type="button"
              className="sidebar-signout"
              onClick={signOut}
              title="Sign out"
              aria-label="Sign out"
            >
              <SignOutIcon />
            </button>
          </div>
        )}
        <div className="sidebar-status-pill">
          <span className="status-dot" style={{ backgroundColor: dotColor, color: dotColor }} />
          <span className="status-label">{statusText}</span>
        </div>
        <div className="sidebar-version">v1.0.0</div>
      </div>
    </nav>
  );
}
