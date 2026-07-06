import { useTranslation } from "react-i18next";

const STATUS_MAP = {
  disconnected: { color: "#9a8d7b", labelKey: "common:status.disconnected" },
  connected: { color: "#12a150", labelKey: "common:status.connected" },
  rendering: { color: "#3f74ff", labelKey: "common:status.rendering" },
  paused: { color: "#f59e0b", labelKey: "common:status.paused" },
  error: { color: "#ef4444", labelKey: "common:status.error" },
  checking_requirements: { color: "#f5a623", labelKey: "dashboard:statusMap.checkingSystem" },
  setting_up_docker: { color: "#f5a623", labelKey: "dashboard:statusMap.settingUpDocker" },
  downloading_image: { color: "#f5a623", labelKey: "dashboard:statusMap.downloadingImage" },
  installing_image: { color: "#f5a623", labelKey: "dashboard:statusMap.installingImage" },
  registering: { color: "#f5a623", labelKey: "dashboard:statusMap.registering" },
  removing_image: { color: "#f59e0b", labelKey: "dashboard:statusMap.removingImage" },
  needs_reboot: { color: "#f59e0b", labelKey: "dashboard:statusMap.rebootRequired" },
};

export default function StatusIndicator({ status, message }) {
  const { t } = useTranslation(["dashboard", "common"]);
  const info = STATUS_MAP[status] || STATUS_MAP.disconnected;

  return (
    <div className="status-indicator">
      <span
        className={`status-dot ${status === "rendering" ? "pulse" : ""}`}
        style={{ backgroundColor: info.color }}
      />
      <div className="status-text">
        <span className="status-state">{t(info.labelKey)}</span>
        {message && <span className="status-message">{message}</span>}
      </div>
    </div>
  );
}
