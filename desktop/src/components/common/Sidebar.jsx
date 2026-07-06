import { useTranslation } from "react-i18next";
import { useAuth } from "../../contexts/AuthContext";
import { useUserProfile } from "../../contexts/UserProfileContext";
import LanguageSelector from "./LanguageSelector";

const STATUS_COLORS = {
  connected: "#12a150",
  rendering: "#3f74ff",
  paused: "#f59e0b",
  error: "#ef4444",
  disconnected: "#9a8d7b",
  checking_requirements: "#f5a623",
  setting_up_docker: "#f5a623",
  downloading_image: "#f5a623",
  registering: "#f5a623",
  needs_reboot: "#f59e0b",
};

const STATUS_LABEL_KEYS = {
  connected: "status.connected",
  rendering: "status.rendering",
  paused: "status.paused",
  disconnected: "status.disconnected",
  error: "status.error",
};

function BrandMark() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 3l7 4v6l-7 4-7-4V7z" fill="#fff" fillOpacity=".92" />
      <path d="M12 9l3 1.7v3L12 15l-3-1.7v-3z" fill="var(--th)" />
    </svg>
  );
}

function ModeIconRent() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="4" width="20" height="13" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </svg>
  );
}

function ModeIconOffer() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="8" rx="2" />
      <rect x="2" y="13" width="20" height="8" rx="2" />
      <path d="M6 7h.01M6 17h.01" />
    </svg>
  );
}

const MODE_OPTIONS = [
  { value: "rentee", labelKey: "mode.rentee", Icon: ModeIconRent },
  { value: "renter", labelKey: "mode.renter", Icon: ModeIconOffer },
];

function NavIconCreate() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 3v5h5" />
      <path d="M6 3h8l5 5v11a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
      <path d="M12 11v6M9 14h6" />
    </svg>
  );
}

function NavIconJobs() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1.6" />
      <rect x="14" y="3" width="7" height="7" rx="1.6" />
      <rect x="3" y="14" width="7" height="7" rx="1.6" />
      <rect x="14" y="14" width="7" height="7" rx="1.6" />
    </svg>
  );
}

function NavIconStats() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 3v18h18" />
      <rect x="7" y="11" width="3" height="7" rx="0.5" />
      <rect x="12" y="7" width="3" height="11" rx="0.5" />
      <rect x="17" y="4" width="3" height="14" rx="0.5" />
    </svg>
  );
}

function NavIconDownloads() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="M7 10l5 5 5-5M12 15V3" />
    </svg>
  );
}

function NavIconMachines() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2.5" />
      <rect x="9" y="9" width="6" height="6" rx="1" />
      <path d="M9 2v2M15 2v2M9 20v2M15 20v2M20 9h2M20 14h2M2 9h2M2 14h2" />
    </svg>
  );
}

function NavIconLogs() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M4 6h16M4 12h16M4 18h11" />
    </svg>
  );
}

function NavIconConfiguration() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
      <circle cx="14" cy="6" r="2.5" fill="var(--bg-primary)" />
      <circle cx="8" cy="12" r="2.5" fill="var(--bg-primary)" />
      <circle cx="16" cy="18" r="2.5" fill="var(--bg-primary)" />
    </svg>
  );
}

function NavIconDashboard() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
      <path d="M3 11l9-8 9 8" />
      <path d="M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5" />
    </svg>
  );
}

function NavIconAbout() {
  return (
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="11" x2="12" y2="17" />
      <circle cx="12" cy="7.5" r="1" fill="currentColor" />
    </svg>
  );
}

function SignOutIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M16 17l5-5-5-5M21 12H9" />
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    </svg>
  );
}

const RENTER_PAGES = [
  { id: "dashboard", labelKey: "nav.dashboard", Icon: NavIconDashboard },
  { id: "logs", labelKey: "nav.logs", Icon: NavIconLogs },
  { id: "about", labelKey: "nav.about", Icon: NavIconAbout },
];

const RENTEE_PAGES = [
  { id: "dashboard", labelKey: "nav.dashboard", Icon: NavIconDashboard },
  { id: "create", labelKey: "nav.create", Icon: NavIconCreate },
  { id: "myjobs", labelKey: "nav.myjobs", Icon: NavIconJobs },
  { id: "stats", labelKey: "nav.stats", Icon: NavIconStats },
  { id: "downloads", labelKey: "nav.downloads", Icon: NavIconDownloads },
  { id: "available", labelKey: "nav.available", Icon: NavIconMachines },
  { id: "configuration", labelKey: "nav.configuration", Icon: NavIconConfiguration },
  { id: "logs", labelKey: "nav.logs", Icon: NavIconLogs },
  { id: "about", labelKey: "nav.about", Icon: NavIconAbout },
];

export default function Sidebar({
  activePage,
  onNavigate,
  status,
  mode,
  onModeChange,
  activeDownloadCount = 0,
}) {
  const { t } = useTranslation("common");
  const { user, signOut } = useAuth();
  const { profile } = useUserProfile();
  const isAdmin = profile?.role === "admin";
  const pages = (mode === "renter" ? RENTER_PAGES : RENTEE_PAGES).filter(
    (p) => p.id !== "configuration" || isAdmin
  );
  const dotColor = STATUS_COLORS[status] || "#9a8d7b";
  const statusKey = STATUS_LABEL_KEYS[status];
  const statusText = statusKey ? t(statusKey) : t("status.settingUp");

  const initial = (user?.email || user?.displayName || "?").trim().charAt(0).toUpperCase();
  const displayName = user?.email || user?.displayName || t("user.signedIn");

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">
        <span className="sidebar-brand-mark">
          <BrandMark />
        </span>
        <span className="sidebar-brand-text">Forge</span>
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
              <span className="nav-label">{t(page.labelKey)}</span>
              {page.id === "downloads" && activeDownloadCount > 0 && (
                <span className="nav-badge">{activeDownloadCount}</span>
              )}
            </button>
          );
        })}
      </div>

      <div className="sidebar-footer">
        <div className="sidebar-mode-switch" role="tablist" aria-label={t("mode.rentee") + " / " + t("mode.renter")}>
          <span
            className={`sidebar-mode-knob${mode === "renter" ? " sidebar-mode-knob-right" : ""}`}
            aria-hidden="true"
          />
          {MODE_OPTIONS.map((opt) => {
            const Icon = opt.Icon;
            const selected = opt.value === mode;
            return (
              <button
                key={opt.value}
                type="button"
                role="tab"
                aria-selected={selected}
                className={`sidebar-mode-tab${selected ? " selected" : ""}`}
                onClick={() => onModeChange(opt.value)}
              >
                <Icon />
                {t(opt.labelKey)}
              </button>
            );
          })}
        </div>

        {user && (
          <div className="sidebar-user-chip">
            <span className="sidebar-avatar" aria-hidden="true">{initial}</span>
            <div className="sidebar-user-meta">
              <div className="sidebar-email" title={user.email || ""}>{displayName}</div>
              <div className="sidebar-user-status">
                <span className="status-dot" style={{ backgroundColor: dotColor, color: dotColor }} />
                <span className="status-label">{statusText}</span>
              </div>
            </div>
            <button
              type="button"
              className="sidebar-signout"
              onClick={signOut}
              title={t("user.signOut")}
              aria-label={t("user.signOut")}
            >
              <SignOutIcon />
            </button>
          </div>
        )}

        <div className="sidebar-meta-row">
          <LanguageSelector />
          <div className="sidebar-version">v1.0.0</div>
        </div>
      </div>
    </nav>
  );
}
