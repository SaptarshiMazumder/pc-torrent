import { useTranslation } from "react-i18next";
import LogViewer from "../components/common/LogViewer";

export default function LogsPage({ logs, onClearLogs }) {
  const { t } = useTranslation(["logs", "common"]);
  return (
    <div className="page logs-page">
      <div className="page-header">
        <div className="page-header-title">
          <div className="page-eyebrow">{t("common:eyebrow.system")}</div>
          <h2>{t("title")}</h2>
        </div>
        <div className="page-header-actions">
          <span className="log-count">{t("entries", { count: logs.length })}</span>
          <button
            className="btn btn-secondary"
            onClick={() => onClearLogs?.()}
            disabled={logs.length === 0}
          >
            {t("clear")}
          </button>
        </div>
      </div>
      <LogViewer logs={logs} />
    </div>
  );
}
