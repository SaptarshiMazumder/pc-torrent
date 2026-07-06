import { useTranslation } from "react-i18next";
import LogViewer from "../components/common/LogViewer";

export default function LogsPage({ logs, onClearLogs }) {
  const { t } = useTranslation("common");
  return (
    <div className="page logs-page">
      <div className="page-header">
        <div className="page-header-title">
          <div className="page-eyebrow">{t("eyebrow.system")}</div>
          <h2>Logs</h2>
        </div>
        <div className="page-header-actions">
          <span className="log-count">{logs.length} entries</span>
          <button
            className="btn btn-secondary"
            onClick={() => onClearLogs?.()}
            disabled={logs.length === 0}
          >
            Clear Logs
          </button>
        </div>
      </div>
      <LogViewer logs={logs} />
    </div>
  );
}
